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
from datetime import datetime
from typing import Callable

from ..db import cursor, heartbeat, insert_injury_if_changed
from ..sources import injuries as injuries_src
from ..timeutil import now_utc
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

def ingest_injury_rows(cur, rows: list[dict], report_utc: datetime, *, source: str = "nba_official") -> tuple[int, int]:
    """把一份報告寫入 injuries。回傳 (新增列數, 無法對應球員的列數)。冪等。"""
    cur.execute("SELECT id, name, team_id FROM players")
    by_name: dict[str, tuple[int, int | None] | None] = {}
    for r in cur.fetchall():
        key = injuries_src.normalize_player_name(r["name"])
        if not key:
            continue
        # 同名球員無法可靠對應 → 標為 None，整批略過而非猜錯人
        by_name[key] = None if key in by_name else (r["id"], r["team_id"])

    written = unmatched = 0
    for row in rows:
        key = injuries_src.normalize_player_name(row.get("player_name"))
        status = row.get("status")
        if not key or not isinstance(status, str) or not status.strip():
            continue  # Not Yet Submitted 等無狀態列
        match = by_name.get(key)
        if not match:
            unmatched += 1
            continue
        player_id, team_id = match
        reason = row.get("reason") if isinstance(row.get("reason"), str) else None
        written += int(insert_injury_if_changed(
            cur, report_time_utc=report_utc, player_id=player_id, team_id=team_id, game_id=None,
            status=status.strip(), reason=reason, source=source,
        ))
    return written, unmatched


def fetch_injuries_job(
    now: datetime | None = None, *, interval_minutes: int = 15, lookback_hours: int = 6,
    check: Callable[[datetime], bool] | None = None,
    fetch: Callable[[datetime], list[dict]] | None = None,
) -> int | None:
    """規格書 §5：比賽日每 30 分鐘（尖峰 15 分鐘）。
    以美東時間往回探測最近一份已發布報告（15 分鐘網格），解析後以 UTC 寫入 DB。
    回傳新增列數；找不到報告 / 來源失敗回 None（只記心跳，不中止排程）。"""
    now = now or now_utc()
    fetch = fetch or injuries_src.fetch_injury_report
    meta = dict(source_key="nbainjuries", display_name="NBA 官方傷病報告", category="injury",
                expected_interval_min=30)

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
