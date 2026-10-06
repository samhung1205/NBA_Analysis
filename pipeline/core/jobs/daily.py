"""
Phase A-6：排程用的抓取工作入口
------------------------------------------------------------
實際邏輯分在：
  - jobs/games_sync.py  賽程 / 即時比分 / 賽後結算（來源 fallback、以 game_time_utc 為準）
  - 本檔的傷病工作       官方傷病報告（美東時間探測、15 分鐘 interval、冪等寫入）

時間換算集中在 core/timeutil.py：DB 一律 UTC，「今日/明日」以台灣時間為準，
呼叫 NBA/ESPN 按日期查詢的 API 時才換成美東日期。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Callable

from ..db import cursor, heartbeat, insert_injury_if_changed, transaction
from ..injury_history import ingest_snapshot, load_directories, parse_report
from ..sources import injuries as injuries_src
from ..timeutil import ET, UTC, now_utc
from .games_sync import SyncReport, run_active_refresh, run_daily_schedule

log = logging.getLogger(__name__)


def fetch_schedule_and_scores_job() -> SyncReport:
    """規格書 §5：每日 12:00（台灣）抓隔日賽程；同時補昨日 ET 殘留 / 今日賽事的比分與結算。"""
    return run_daily_schedule()


def refresh_live_and_final_job() -> SyncReport | None:
    """即時比分 / 賽後結算：依 DB 內 game_time_utc 判斷，沒有進行中或待結算賽事就不打外部來源。"""
    return run_active_refresh()


# ------------------------------------------------------------------ #
# 傷病                                                                  #
# ------------------------------------------------------------------ #

NOT_LISTED_REASON = "Not listed on latest official report"


def _game_id_for(cur, cache: dict, team_id: int | None, game_date) -> int | None:
    """球隊 + 賽事日（ET）→ games.id（賽程尚未同步則 None）。"""
    if team_id is None:
        return None
    key = (team_id, game_date)
    if key not in cache:
        start = datetime.combine(game_date, time.min, tzinfo=ET).astimezone(UTC)
        cur.execute(
            "SELECT id FROM games WHERE (home_team_id = %s OR away_team_id = %s)"
            " AND date_utc >= %s AND date_utc < %s ORDER BY date_utc LIMIT 1",
            (team_id, team_id, start, start + timedelta(days=1)))
        row = cur.fetchone()
        cache[key] = row["id"] if row else None
    return cache[key]


def ingest_injury_rows(cur, rows: list[dict], report_utc: datetime, *, source: str = "nba_official") -> tuple[int, int]:
    """把一份報告寫入 DB。回傳 (injuries 新增列數, 無法對應球員的列數)。冪等、可並行。

    兩個層次：
      1. 完整快照（injury_reports / injury_report_entries）：歷史 as-of 還原的權威來源，單一交易寫入。
      2. injuries（API 讀取「每位球員最新狀態」）：只在狀態變化時新增一列；每隊只取報告中**最早的賽事日**，
         避免同一球員因多個賽事日列在同一份報告而互相覆寫。球員在「已申報」的隊伍中不再出現
         → 補一列 Available（reason='Not listed on latest official report'），舊的 Out/Doubtful 不再永久保留。
         NOT YET SUBMITTED 的隊伍不動（沒有資訊，不是康復）。
         若已有「更新」的報告寫入過，這份（補抓的舊報告）只進快照、不動 injuries。
    """
    teams, players = load_directories(cur)
    parsed = parse_report(rows, report_utc, teams, players)
    with transaction() as tcur:
        ingest_snapshot(tcur, parsed, source=source)

    # 較新的報告已經寫入過 → 這份是補抓的舊報告：只進快照，不動「最新狀態」表，
    # 否則舊報告的 Out 會蓋過新報告「球員已不在名單上」的事實（older snapshot overwriting newer state）。
    cur.execute("SELECT 1 FROM injury_reports WHERE source = %s AND report_time_utc > %s LIMIT 1",
                (source, report_utc))
    if cur.fetchone():
        return 0, parsed.n_unmatched

    team_first_date: dict[str, str] = {}
    for iso in sorted(parsed.coverage):
        for abbr in parsed.coverage[iso]:
            team_first_date.setdefault(abbr, iso)
    listed: dict[str, set[int]] = {}
    gid_cache: dict = {}
    written = 0
    for e in parsed.entries:
        if e.player_id is None or e.team_abbr is None or e.game_date.isoformat() != team_first_date.get(e.team_abbr):
            continue
        listed.setdefault(e.team_abbr, set()).add(e.player_id)
        written += int(insert_injury_if_changed(
            cur, report_time_utc=report_utc, player_id=e.player_id, team_id=e.team_id,
            game_id=_game_id_for(cur, gid_cache, e.team_id, e.game_date), status=e.status,
            reason=e.reason, source=source))

    # 球員消失 → Available
    submitted = {abbr for abbr, iso in team_first_date.items() if not parsed.coverage[iso][abbr].get("nys")}
    if submitted:
        cur.execute(
            """
            SELECT DISTINCT ON (player_id) player_id, team_id, status FROM injuries
             WHERE source = %s AND report_time_utc < %s AND player_id IS NOT NULL
             ORDER BY player_id, report_time_utc DESC, id DESC
            """,
            (source, report_utc))
        abbr_by_id = {v: k for k, v in teams.abbr_to_id.items()}
        for r in cur.fetchall():
            abbr = abbr_by_id.get(r["team_id"])
            if abbr not in submitted or r["status"] == "Available" or r["player_id"] in listed.get(abbr, set()):
                continue
            iso = team_first_date[abbr]
            written += int(insert_injury_if_changed(
                cur, report_time_utc=report_utc, player_id=r["player_id"], team_id=r["team_id"],
                game_id=_game_id_for(cur, gid_cache, r["team_id"], date.fromisoformat(iso)),
                status="Available", reason=NOT_LISTED_REASON, source=source))
    return written, parsed.n_unmatched


def injury_poll_job(now: datetime | None = None, *, fetch_job: Callable[..., int | None] | None = None) -> int | None:
    """排程進入點（scheduler-efficiency-v1）：APScheduler 照舊每 15 / 30 分鐘醒來，這裡先用 DB 判斷要不要打外部來源。
    依「下一場尚未開賽的 production-eligible 比賽」決定輪詢間隔（>36h 不抓；12–36h ≤ 每 120 分；3–12h ≤ 每 60 分；
    0–3h ≤ 每 15 分），見 production/activity.py。被略過是正常狀態（只記 log，不寫心跳、不算錯誤）。
    報告時間 / ET 探測 / 寫入語意全部沿用 fetch_injuries_job，不變。"""
    from ..production import activity

    now = now or now_utc()
    with cursor() as cur:
        dec = activity.injury_state(cur, now)
        activity.sync_expected_interval(cur, "nbainjuries", dec.interval_min)
    if not dec.run:
        nxt = f"next_game={dec.next_game_utc:%Y-%m-%dT%H:%MZ} in {dec.hours_to_next:.1f}h" if dec.next_game_utc else ""
        activity.log_skip("injuries", dec.reason, nxt)
        return None
    return (fetch_job or fetch_injuries_job)(now, expected_interval_min=dec.interval_min)


def fetch_injuries_job(
    now: datetime | None = None, *, interval_minutes: int = 15, lookback_hours: int = 6,
    check: Callable[[datetime], bool] | None = None,
    fetch: Callable[[datetime], list[dict]] | None = None,
    expected_interval_min: int = 30,
) -> int | None:
    """規格書 §5：比賽日每 30 分鐘（尖峰 15 分鐘）。
    以美東時間往回探測最近一份已發布報告（15 分鐘網格），解析後以 UTC 寫入 DB。
    回傳新增列數；找不到報告 / 來源失敗回 None（只記心跳，不中止排程）。"""
    now = now or now_utc()
    fetch = fetch or injuries_src.fetch_injury_report
    meta = dict(source_key="nbainjuries", display_name="NBA 官方傷病報告", category="injury",
                expected_interval_min=expected_interval_min)

    found_et = injuries_src.find_latest_report(
        now, interval_minutes=interval_minutes, lookback_hours=lookback_hours, check=check)
    if not found_et:
        with cursor() as cur:
            heartbeat(cur, status="warn", error=f"近 {lookback_hours} 小時內查無已發布報告（非賽季屬正常）", **meta)
        log.info("傷病：近 %d 小時內查無已發布報告", lookback_hours)
        return None

    try:
        rows = fetch(found_et)
    except Exception as e:  # noqa: BLE001 — 解析失敗（Java/PDF 格式變動）不應讓排程中止
        log.exception("傷病報告 %s 解析失敗", found_et)
        with cursor() as cur:
            heartbeat(cur, status="error", error=f"解析 {found_et:%Y-%m-%d %H:%M} ET 報告失敗：{e}"[:500], **meta)
        return None

    report_utc = injuries_src.report_time_utc(found_et)
    with cursor() as cur:
        written, unmatched = ingest_injury_rows(cur, rows, report_utc)
        heartbeat(cur, status="ok", records_updated=written,
                  error=f"{unmatched} 筆球員姓名無法對應" if unmatched else None, **meta)
    log.info("傷病報告 %s ET（= %s UTC）：共 %d 列，新增/變更 %d，未對應 %d",
             f"{found_et:%Y-%m-%d %H:%M}", f"{report_utc:%Y-%m-%d %H:%M}", len(rows), written, unmatched)
    return written
