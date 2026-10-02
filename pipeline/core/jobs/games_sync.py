"""
賽程 / 即時比分 / 賽後結算 同步（C.5A：來源 fallback + 以 game_time_utc 為準）
------------------------------------------------------------
來源優先序（每個階段各自 fallback，單一來源失敗不中止 pipeline）：

  賽程/比分/逐節  nba_cdn（整季賽程 + 當日 scoreboard）→ nba_api（ScoreboardV3）→ espn
  基本 box score  nba_cdn（boxscore_<id>.json）→ nba_api（BoxScoreTraditionalV3）
  進階數據        nba_api（BoxScoreAdvancedV3），CDN 沒有；失敗不影響主流程，下次再補

要刷新哪些比賽由 UTC 視窗決定（見 timeutil.refresh_window_utc），視窗內是否收錄以
比賽實際 game_time_utc 判定；ET 日期只用來呼叫「按日期查詢」的 API。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..db import (
    cursor, find_game_by_matchup, GAME_COLUMNS, get_game_id_by_nba_id, upsert_game, adopt_espn_game,
    upsert_player, upsert_player_game_stats, upsert_team_game_stats,
)
from ..sources import espn, nba_cdn, nba_stats
from ..sources.fallback import AllSourcesFailed, Attempt, fetch_with_fallback, report_attempts
from ..timeutil import et_dates_between, in_window, now_utc, refresh_window_utc

log = logging.getLogger(__name__)

PLAYER_FIELDS = ("min", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga",
                 "fg3m", "fg3a", "ftm", "fta", "plus_minus", "started")


@dataclass
class SyncReport:
    source: str | None = None            # 提供賽程的來源
    fallback_used: bool = False
    games_upserted: int = 0
    games_skipped: int = 0
    box_basic_done: int = 0
    box_advanced_done: int = 0
    attempts: list[Attempt] = field(default_factory=list)
    error: str | None = None


# ------------------------------------------------------------------ #
# 賽程 / 比分                                                           #
# ------------------------------------------------------------------ #

def _schedule_chain(start_utc: datetime, end_utc: datetime):
    dates = et_dates_between(start_utc, end_utc)

    def windowed(fn):
        return lambda: [g for g in fn(dates) if in_window(g["date_utc"], start_utc, end_utc)]

    return [
        ("nba_cdn", lambda: nba_cdn.fetch_games_in_window(start_utc, end_utc)),
        ("nba_api", windowed(nba_stats.fetch_games_for_et_dates)),
        ("espn", windowed(espn.fetch_games_for_et_dates)),
    ]


def _team_maps(cur) -> tuple[dict[int, int], dict[str, int]]:
    cur.execute("SELECT id, nba_team_id, abbr FROM teams")
    rows = cur.fetchall()
    return ({r["nba_team_id"]: r["id"] for r in rows if r["nba_team_id"]},
            {r["abbr"]: r["id"] for r in rows if r["abbr"]})


def upsert_normalized_game(cur, g: dict, by_nba_id: dict[int, int], by_abbr: dict[str, int]) -> int | None:
    """寫入一場正規化賽事；回傳本地 games.id，無法對應球隊則 None。冪等。

    - 官方 ID 先認領同場的 'espn:' 暫存列（沿用既有 adopt 邏輯），避免重複。
    - ESPN 備援（'espn:<id>'）若官方來源已建立同場，沿用該列的 nba_game_id，不新增第二列。
    - 不讓已 final 的賽事被較舊的非 final 資料（如快取中的 schedule 檔）倒退。
    """
    home_id = by_nba_id.get(g.get("home_nba_team_id")) or by_abbr.get(g.get("home_abbr"))
    away_id = by_nba_id.get(g.get("away_nba_team_id")) or by_abbr.get(g.get("away_abbr"))
    if not home_id or not away_id:
        log.warning("找不到隊伍對應 %s @ %s，略過 %s", g.get("away_abbr"), g.get("home_abbr"), g["nba_game_id"])
        return None

    game_key = g["nba_game_id"]
    if game_key.startswith("espn:"):
        existing = find_game_by_matchup(cur, home_team_id=home_id, away_team_id=away_id,
                                        date_utc=g["date_utc"])
        if existing:
            game_key = existing["nba_game_id"]
    else:
        adopt_espn_game(cur, nba_game_id=game_key, home_team_id=home_id, away_team_id=away_id,
                        date_utc=g["date_utc"])

    cur.execute("SELECT status FROM games WHERE nba_game_id = %s", (game_key,))
    row = cur.fetchone()
    if row and row["status"] == "final" and g["status"] != "final":
        return get_game_id_by_nba_id(cur, game_key)

    fields = {k: v for k, v in g.items() if k in GAME_COLUMNS}
    fields.update(home_team_id=home_id, away_team_id=away_id)
    return upsert_game(cur, nba_game_id=game_key, **fields)


def sync_games(start_utc: datetime, end_utc: datetime) -> SyncReport:
    """刷新 [start_utc, end_utc) 內（以 game_time_utc 判定）的賽程/比分，並補齊賽後 box score。"""
    report = SyncReport()
    try:
        result = fetch_with_fallback("schedule", _schedule_chain(start_utc, end_utc))
    except AllSourcesFailed as e:
        report.attempts, report.error = e.attempts, str(e)
        report_attempts("schedule", e.attempts)
        log.error("賽程同步失敗：%s", e)
        return report

    report.attempts, report.source, report.fallback_used = result.attempts, result.source, result.fallback_used
    with cursor() as cur:
        by_nba_id, by_abbr = _team_maps(cur)
        for g in result.data:
            if upsert_normalized_game(cur, g, by_nba_id, by_abbr):
                report.games_upserted += 1
            else:
                report.games_skipped += 1
    report_attempts("schedule", result.attempts)

    _sync_box_scores(start_utc, end_utc, report)
    return report


# ------------------------------------------------------------------ #
# 賽後 box score                                                        #
# ------------------------------------------------------------------ #

def games_needing_box(cur, start_utc: datetime, end_utc: datetime) -> list[dict]:
    """final 且（沒有球隊數據 / 沒有逐節 / 進階數據缺漏）的賽事。espn: 暫存賽事無法抓 box，排除。"""
    cur.execute(
        """
        SELECT g.id, g.nba_game_id, g.home_team_id, g.away_team_id,
               NOT EXISTS (SELECT 1 FROM team_game_stats t WHERE t.game_id = g.id) OR g.home_q1 IS NULL
                 AS needs_basic,
               EXISTS (SELECT 1 FROM team_game_stats t WHERE t.game_id = g.id AND t.pace IS NULL)
                 OR NOT EXISTS (SELECT 1 FROM team_game_stats t WHERE t.game_id = g.id)
                 AS needs_advanced
          FROM games g
         WHERE g.status = 'final' AND g.nba_game_id NOT LIKE 'espn:%%'
           AND g.date_utc >= %s AND g.date_utc < %s
           AND (NOT EXISTS (SELECT 1 FROM team_game_stats t WHERE t.game_id = g.id)
                OR g.home_q1 IS NULL
                OR EXISTS (SELECT 1 FROM team_game_stats t WHERE t.game_id = g.id AND t.pace IS NULL))
         ORDER BY g.date_utc
        """,
        (start_utc, end_utc),
    )
    return cur.fetchall()


def _store_basic(cur, game: dict, box: dict) -> None:
    for side, team_id in (("home", game["home_team_id"]), ("away", game["away_team_id"])):
        upsert_team_game_stats(cur, game_id=game["id"], team_id=team_id,
                               is_home=1 if side == "home" else 0, **box[side]["team"])
        for p in box[side]["players"]:
            pid = upsert_player(cur, nba_player_id=p["nba_player_id"], name=p["name"], team_id=team_id,
                                position=p["position"], is_starter=p["started"])
            upsert_player_game_stats(cur, game_id=game["id"], player_id=pid, team_id=team_id,
                                     **{k: p[k] for k in PLAYER_FIELDS if k in p})
    if box.get("quarters"):
        q = {k: v for k, v in box["quarters"].items() if k in GAME_COLUMNS}
        sets = ", ".join(f"{k} = %s" for k in q)
        cur.execute(f"UPDATE games SET {sets}, updated_at = NOW() WHERE id = %s", [*q.values(), game["id"]])


def _store_advanced(cur, game: dict, adv: dict) -> None:
    for side, team_id in (("home", game["home_team_id"]), ("away", game["away_team_id"])):
        upsert_team_game_stats(cur, game_id=game["id"], team_id=team_id,
                               is_home=1 if side == "home" else 0, **adv[side])


def _sync_box_scores(start_utc: datetime, end_utc: datetime, report: SyncReport) -> None:
    with cursor() as cur:
        todo = games_needing_box(cur, start_utc, end_utc)
    last_basic: list[Attempt] = []
    last_adv: list[Attempt] = []
    basic_failed: list[Attempt] = []
    adv_failed: list[Attempt] = []

    for game in todo:
        gid = game["nba_game_id"]
        if game["needs_basic"]:
            try:
                res = fetch_with_fallback("box_basic", [
                    ("nba_cdn", lambda: nba_cdn.fetch_box_basic(gid)),
                    ("nba_api", lambda: nba_stats.fetch_box_traditional(gid)),
                ])
                last_basic = res.attempts
                with cursor() as cur:
                    _store_basic(cur, game, res.data)
                report.box_basic_done += 1
            except AllSourcesFailed as e:
                basic_failed = e.attempts
                log.warning("box score（基本）%s 全部來源失敗，下次排程重試", gid)
                continue  # 沒有基本數據就不必抓進階
        if game["needs_advanced"]:
            try:
                res = fetch_with_fallback("box_advanced", [("nba_api", lambda: nba_stats.fetch_box_advanced(gid))])
                last_adv = res.attempts
                with cursor() as cur:
                    _store_advanced(cur, game, res.data)
                report.box_advanced_done += 1
            except AllSourcesFailed as e:
                adv_failed = e.attempts  # 進階數據缺漏不阻斷主流程；team_game_stats.pace 仍為 NULL，下次補

    if basic_failed or last_basic:
        report_attempts("box_basic", basic_failed or last_basic)
    if adv_failed or last_adv:
        report_attempts("box_advanced", adv_failed or last_adv, optional=True)


# ------------------------------------------------------------------ #
# Job 入口                                                             #
# ------------------------------------------------------------------ #

def run_daily_schedule(now: datetime | None = None) -> SyncReport:
    """每日 12:00（台灣）：昨日 ET 殘留賽事 + 今日 + 明日（台灣時間）。"""
    start, end = refresh_window_utc(now)
    report = sync_games(start, end)
    log.info("每日賽程/比分：來源=%s%s，賽事 %d（略過 %d），box 基本 %d / 進階 %d%s",
             report.source, "（備援）" if report.fallback_used else "", report.games_upserted,
             report.games_skipped, report.box_basic_done, report.box_advanced_done,
             f"，錯誤：{report.error}" if report.error else "")
    return report


def active_games_window(cur, now: datetime) -> tuple[datetime, datetime] | None:
    """依 DB 內賽事的 game_time_utc 決定「現在需要刷新」的視窗；沒有就回 None（完全不打外部來源）。
    - 已該開賽（開賽 ≤ now + 15 分）但尚未 final，且開賽不超過 8 小時前（進行中 / 剛結束待結算）
    - final 但基本 box score / 逐節未補齊的近 48 小時賽事（賽後結算）。
      進階數據（pace 等）缺漏只留給每日工作補，避免被封鎖時每 5 分鐘白打一輪。"""
    cur.execute(
        """
        SELECT min(date_utc) AS lo, max(date_utc) AS hi FROM games g
         WHERE g.nba_game_id NOT LIKE 'espn:%%' AND (
               (g.status <> 'final' AND g.date_utc <= %s::timestamptz + interval '15 minutes'
                                     AND g.date_utc >= %s::timestamptz - interval '8 hours')
            OR (g.status = 'final' AND g.date_utc >= %s::timestamptz - interval '48 hours'
                AND (NOT EXISTS (SELECT 1 FROM team_game_stats t WHERE t.game_id = g.id)
                     OR g.home_q1 IS NULL)))
        """,
        (now, now, now),
    )
    row = cur.fetchone()
    if not row or row["lo"] is None:
        return None
    return row["lo"] - timedelta(minutes=1), row["hi"] + timedelta(minutes=1)


def run_active_refresh(now: datetime | None = None) -> SyncReport | None:
    """即時比分 / 賽後結算：只在 DB 裡有「依 game_time_utc 看應該在進行或待結算」的比賽時才動作。"""
    now = now or now_utc()
    with cursor() as cur:
        window = active_games_window(cur, now)
    if window is None:
        log.debug("無進行中/待結算賽事，略過刷新")
        return None
    report = sync_games(*window)
    log.info("即時/結算刷新：來源=%s，賽事 %d，box 基本 %d / 進階 %d",
             report.source, report.games_upserted, report.box_basic_done, report.box_advanced_done)
    return report
