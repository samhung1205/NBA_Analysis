"""
Phase A-5：回填近 N 季歷史資料（含逐節比分，供半場模型）
------------------------------------------------------------
可安全地中斷後重跑：team/game/box score 全走 UPSERT，且逐場 box
score（較耗時的部分）在寫入前會先檢查是否已存在，已存在就跳過。
"""
from __future__ import annotations

import argparse
import logging
import time

from ..db import (
    cursor, upsert_team, upsert_game, upsert_team_game_stats, upsert_player_game_stats,
    upsert_player, get_game_id_by_nba_id, heartbeat, adopt_espn_game,
)
from ..sources import nba_stats

log = logging.getLogger(__name__)

GAME_QUARTER_FIELDS = [
    "home_q1", "home_q2", "home_q3", "home_q4", "home_ot", "home_h1", "home_h2",
    "away_q1", "away_q2", "away_q3", "away_q4", "away_ot", "away_h1", "away_h2",
]

# 階段一 seed script（seed/seed.template.sql）用來讓 UI 有假資料可測的
# 17 場示範賽事 nba_game_id。真實回填開始前必須先清掉，否則會被回測
# 誤判為真實比賽。teams/players/users/bets 不受影響（seed 的球隊用真實
# nba_team_id，upsert 時會被真實資料自然覆蓋合併，不需特別處理）。
SEED_DEMO_GAME_IDS = [
    "0022500001", "0022500002", "0022500003", "0022500004", "0022500005",
    "0022500006", "0022500007", "0022500008", "0022500009", "0022500010",
    "0022500011", "0022500012", "0022500098", "0022500099", "0022500101",
    "0022500102", "0022500103",
]


def clean_seed_demo_games() -> None:
    with cursor() as cur:
        cur.execute("SELECT id FROM games WHERE nba_game_id = ANY(%s)", (SEED_DEMO_GAME_IDS,))
        ids = [r["id"] for r in cur.fetchall()]
        if not ids:
            return
        cur.execute("DELETE FROM injuries WHERE game_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM games WHERE id = ANY(%s)", (ids,))
    log.info("已清除 %d 場階段一 seed 示範假賽事", len(ids))


def upsert_all_teams() -> dict[int, int]:
    """回傳 {nba_team_id: local_id}"""
    mapping = {}
    with cursor() as cur:
        for t in nba_stats.fetch_static_teams():
            local_id = upsert_team(
                cur, nba_team_id=t["nba_team_id"], abbr=t["abbr"], name=t["name"],
                conference=t["conference"], division=t["division"],
            )
            mapping[t["nba_team_id"]] = local_id
    log.info("球隊 upsert 完成：%d 隊", len(mapping))
    return mapping


def _game_has_box_stats(cur, local_game_id: int) -> bool:
    cur.execute("SELECT 1 FROM team_game_stats WHERE game_id = %s LIMIT 1", (local_game_id,))
    return cur.fetchone() is not None


def _store_box_score(local_game_id: int, nba_game_id: str, home_id: int, away_id: int) -> None:
    trad = nba_stats.fetch_box_traditional(nba_game_id)
    adv = nba_stats.fetch_box_advanced(nba_game_id)

    with cursor() as cur:
        for side, team_id in (("home", home_id), ("away", away_id)):
            merged = {**trad[side]["team"], **adv[side]}
            merged["is_home"] = 1 if side == "home" else 0
            upsert_team_game_stats(cur, game_id=local_game_id, team_id=team_id, **merged)

            for p in trad[side]["players"]:
                player_local_id = upsert_player(
                    cur, nba_player_id=p["nba_player_id"], name=p["name"], team_id=team_id,
                    position=p["position"], is_starter=p["started"],
                )
                fields = {k: v for k, v in p.items()
                          if k in ("min", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga",
                                    "fg3m", "fg3a", "ftm", "fta", "plus_minus", "started")}
                upsert_player_game_stats(cur, game_id=local_game_id, player_id=player_local_id,
                                          team_id=team_id, **fields)


def process_date(date_str: str, season: str, stage: str, team_map: dict[int, int]) -> int:
    games = nba_stats.fetch_scoreboard_day(date_str, season, stage)
    box_fetched = 0
    for g in games:
        home_id = team_map.get(g["home_nba_team_id"])
        away_id = team_map.get(g["away_nba_team_id"])
        if not home_id or not away_id:
            log.warning("找不到隊伍對應 (nba_team_id home=%s away=%s)，略過 game %s",
                        g["home_nba_team_id"], g["away_nba_team_id"], g["nba_game_id"])
            continue

        with cursor() as cur:
            adopt_espn_game(cur, nba_game_id=g["nba_game_id"], home_team_id=home_id,
                            away_team_id=away_id, date_utc=g["date_utc"])
            local_game_id = upsert_game(
                cur, nba_game_id=g["nba_game_id"], season=season, season_stage=stage,
                date_utc=g["date_utc"], home_team_id=home_id, away_team_id=away_id,
                status=g["status"], home_pts=g["home_pts"], away_pts=g["away_pts"],
                **{k: g.get(k) for k in GAME_QUARTER_FIELDS},
            )
            already_have_box = g["status"] == "final" and _game_has_box_stats(cur, local_game_id)

        if g["status"] == "final" and not already_have_box:
            _store_box_score(local_game_id, g["nba_game_id"], home_id, away_id)
            box_fetched += 1

    return box_fetched


def backfill_season(season: str, team_map: dict[int, int],
                     season_types: tuple[str, ...] = ("Regular Season", "Playoffs")) -> None:
    for season_type in season_types:
        stage = "regular" if season_type == "Regular Season" else "playoffs"
        try:
            df = nba_stats.fetch_season_team_games(season, season_type)
        except Exception:
            # 連該季的賽程清單都抓不到（重試 4 次仍失敗，通常是 stats.nba.com
            # 間歇性異常）：略過這個 season_type，不中斷整個多賽季回填工作。
            log.exception("%s %s：抓取賽程清單失敗，略過（可重跑腳本補齊）", season, season_type)
            continue
        if df.empty:
            log.info("%s %s：無資料，略過", season, season_type)
            continue
        dates = nba_stats.season_game_dates(df)
        log.info("%s %s：%d 個比賽日", season, season_type, len(dates))

        t0 = time.time()
        total_box = 0
        failed_dates: list[str] = []
        for i, date_str in enumerate(dates, 1):
            try:
                n = process_date(date_str, season, stage, team_map)
                total_box += n
            except Exception:
                # 單一日期失敗（例如 stats.nba.com 間歇性逾時）不該中斷整個
                # 多小時的回填工作：記錄下來、跳過，整支腳本可重跑會自動補齊
                # 漏掉的日期（games/box score 皆走 UPSERT，不會重複）。
                log.exception("  [%s %s] %s 抓取失敗，略過（可重跑腳本補齊）",
                              season, season_type, date_str)
                failed_dates.append(date_str)
            if i % 10 == 0 or i == len(dates):
                elapsed = time.time() - t0
                log.info("  [%s %s] 進度 %d/%d 天，本輪已抓 box score %d 場，失敗 %d 天，耗時 %.0fs",
                          season, season_type, i, len(dates), total_box, len(failed_dates), elapsed)
        if failed_dates:
            log.warning("%s %s：%d 個日期失敗，需要重跑補齊：%s",
                        season, season_type, len(failed_dates), failed_dates)


def main() -> None:
    from ..logging_conf import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(description="回填 NBA 歷史資料")
    parser.add_argument("--seasons", nargs="+", default=[
        "2021-22", "2022-23", "2023-24", "2024-25", "2025-26",
    ])
    parser.add_argument("--season-types", nargs="+", default=["Regular Season", "Playoffs"])
    args = parser.parse_args()

    clean_seed_demo_games()
    team_map = upsert_all_teams()

    failed_seasons: list[str] = []
    for season in args.seasons:
        log.info("=== 開始回填 %s ===", season)
        try:
            backfill_season(season, team_map, tuple(args.season_types))
            with cursor() as cur:
                heartbeat(cur, source_key="nba_api", display_name="NBA Stats API",
                          category="games", status="ok", records_updated=None,
                          expected_interval_min=60 * 24)
        except Exception as e:
            # 單一賽季整個失敗也不該中斷其餘賽季的回填；記錄下來，
            # 整支腳本本身可安全重跑（全部走 UPSERT），之後補跑失敗的賽季即可。
            log.exception("%s 回填中斷：%s", season, e)
            failed_seasons.append(season)
            with cursor() as cur:
                heartbeat(cur, source_key="nba_api", display_name="NBA Stats API",
                          category="games", status="error", error=str(e)[:500])

    if failed_seasons:
        log.warning("=== 回填結束，以下賽季未完成，需要重跑：%s ===", failed_seasons)
    else:
        log.info("=== 回填全部完成 ===")


if __name__ == "__main__":
    main()
