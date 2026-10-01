"""
Phase A-6：每日排程用的抓取工作
------------------------------------------------------------
與階段一 src/lib/time.ts 用同一套台灣時間換算邏輯：DB 一律存 UTC，
「今日/明日」以台灣時間 (UTC+8) 為準。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..db import cursor, heartbeat
from ..sources import injuries as injuries_src
from ..sources import nba_stats
from .backfill import GAME_QUARTER_FIELDS, _game_has_box_stats, _store_box_score, upsert_all_teams

log = logging.getLogger(__name__)

TPE = timezone(timedelta(hours=8))


def tpe_today() -> str:
    return datetime.now(TPE).strftime("%Y-%m-%d")


def tpe_tomorrow() -> str:
    return (datetime.now(TPE) + timedelta(days=1)).strftime("%Y-%m-%d")


def _current_season_label(d: datetime) -> str:
    """10 月開季，隔年 6 月結束 → 10~12 月屬當年開頭的賽季，1~9 月屬前一年開頭"""
    year = d.year if d.month >= 10 else d.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


def fetch_schedule_and_scores_job() -> None:
    """規格書 §5：抓隔日賽程 + 歷史對戰，每日 12:00；這裡一併補抓今日（供即時比分/賽後結算）"""
    team_map = {t["nba_team_id"]: None for t in nba_stats.fetch_static_teams()}
    with cursor() as cur:
        cur.execute("SELECT nba_team_id, id FROM teams WHERE nba_team_id IS NOT NULL")
        team_map = {r["nba_team_id"]: r["id"] for r in cur.fetchall()}

    total = 0
    for date_str in (tpe_today(), tpe_tomorrow()):
        season = _current_season_label(datetime.strptime(date_str, "%Y-%m-%d"))
        games = nba_stats.fetch_scoreboard_day(date_str, season, "regular")
        with cursor() as cur:
            from ..db import upsert_game
            for g in games:
                home_id = team_map.get(g["home_nba_team_id"])
                away_id = team_map.get(g["away_nba_team_id"])
                if not home_id or not away_id:
                    continue
                upsert_game(
                    cur, nba_game_id=g["nba_game_id"], season=season, season_stage="regular",
                    date_utc=g["date_utc"], home_team_id=home_id, away_team_id=away_id,
                    status=g["status"], home_pts=g["home_pts"], away_pts=g["away_pts"],
                    **{k: g.get(k) for k in GAME_QUARTER_FIELDS},
                )
                total += 1
                if g["status"] == "final":
                    from ..db import get_game_id_by_nba_id
                    local_id = get_game_id_by_nba_id(cur, g["nba_game_id"])
                    if local_id and not _game_has_box_stats(cur, local_id):
                        _store_box_score(local_id, g["nba_game_id"], home_id, away_id)

        with cursor() as cur:
            heartbeat(cur, source_key="nba_api", display_name="NBA Stats API", category="games",
                      status="ok", records_updated=total, expected_interval_min=60 * 24)
    log.info("每日賽程/比分工作完成，共處理 %d 場", total)


def fetch_injuries_job() -> None:
    """規格書 §5：比賽日每 30 分鐘。用目前時間找最近一次已發布的官方報告。"""
    now = datetime.now(timezone.utc)
    ts = now.replace(minute=0, second=0, microsecond=0)
    found = None
    for _ in range(12):  # 官方報告非整點發布，往回找最近 12 小時內的整點報告
        if injuries_src.check_report_valid(ts):
            found = ts
            break
        ts -= timedelta(hours=1)

    if not found:
        with cursor() as cur:
            heartbeat(cur, source_key="nbainjuries", display_name="NBA 官方傷病報告",
                      category="injuries", status="warn", error="近 12 小時內查無已發布報告",
                      expected_interval_min=30)
        return

    rows = injuries_src.fetch_injury_report(found)
    written = 0
    with cursor() as cur:
        cur.execute("SELECT id, name, team_id FROM players")
        by_name = {r["name"].strip().lower(): (r["id"], r["team_id"]) for r in cur.fetchall()}
        from ..db import insert_injury_if_changed
        for row in rows:
            key = row["player_name"].strip().lower()
            match = by_name.get(key)
            if not match:
                continue  # 找不到對應球員（可能是名字格式差異或未在 DB），略過
            player_id, team_id = match
            changed = insert_injury_if_changed(
                cur, report_time_utc=found.isoformat(), player_id=player_id, team_id=team_id,
                game_id=None, status=row["status"], reason=row["reason"], source="nba_official",
            )
            written += int(changed)
        heartbeat(cur, source_key="nbainjuries", display_name="NBA 官方傷病報告",
                  category="injuries", status="ok", records_updated=written,
                  expected_interval_min=30)
    log.info("傷病報告工作完成，%s 筆新增/變更", written)
