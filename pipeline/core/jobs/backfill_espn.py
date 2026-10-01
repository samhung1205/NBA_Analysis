"""
ESPN 備援回填：賽程 / 比分 / 逐節比分（不含 box score 與進階數據）
------------------------------------------------------------
當 stats.nba.com 的 /stats/* 對本機 IP 被 Akamai 靜默丟棄時使用。足以支撐
Elo walk-forward 回測與半場模型的目標值。games.nba_game_id 暫存為
"espn:<event_id>"；之後 NBA 官方回填（backfill.py）寫入同一場比賽時，
會依 (主客隊, 日期±1天) 認領並改寫成真正的 nba_game_id，不會產生重複。
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta

from ..db import cursor, upsert_game, heartbeat
from ..sources import espn

log = logging.getLogger(__name__)

STATUS_MAP = {"STATUS_FINAL": "final", "STATUS_SCHEDULED": "scheduled", "STATUS_IN_PROGRESS": "live"}


def season_range(season: str) -> tuple[date, date]:
    start_year = int(season[:4])
    return date(start_year, 10, 1), date(start_year + 1, 6, 30)


def _quarters(side: str, scores: list[int]) -> dict:
    q = {f"{side}_q{i}": (scores[i - 1] if len(scores) >= i else None) for i in range(1, 5)}
    q[f"{side}_ot"] = sum(scores[4:]) if len(scores) > 4 else None
    q[f"{side}_h1"] = q[f"{side}_q1"] + q[f"{side}_q2"] if q[f"{side}_q1"] is not None and q[f"{side}_q2"] is not None else None
    q[f"{side}_h2"] = q[f"{side}_q3"] + q[f"{side}_q4"] if q[f"{side}_q3"] is not None and q[f"{side}_q4"] is not None else None
    return q


def season_label(d: datetime) -> str:
    y = d.year if d.month >= 10 else d.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


def process_day(d: date, abbr_to_id: dict[str, int]) -> int:
    events = espn.fetch_scoreboard(d.strftime("%Y%m%d"))
    written = 0
    with cursor() as cur:
        for ev in events:
            stage = espn.STAGE_BY_TYPE.get(ev["season_type"])
            status = STATUS_MAP.get(ev["status"])
            home_id, away_id = abbr_to_id.get(ev["home_abbr"]), abbr_to_id.get(ev["away_abbr"])
            if not stage or not status or not home_id or not away_id:
                continue  # 季前賽、明星賽、延賽、非 NBA 球隊
            when = datetime.fromisoformat(ev["date_utc"].replace("Z", "+00:00"))
            done = status == "final"
            upsert_game(
                cur, nba_game_id=f"espn:{ev['espn_event_id']}", season=season_label(when),
                season_stage=stage, date_utc=when, home_team_id=home_id, away_team_id=away_id,
                status=status,
                home_pts=ev["home_score"] if done else None, away_pts=ev["away_score"] if done else None,
                **(_quarters("home", ev["home_linescores"]) if done else {}),
                **(_quarters("away", ev["away_linescores"]) if done else {}),
            )
            written += 1
    return written


def backfill_season(season: str, abbr_to_id: dict[str, int], delay: float = 0.3) -> tuple[int, list[str]]:
    start, end = season_range(season)
    end = min(end, date.today())
    total, failed, d, i = 0, [], start, 0
    t0 = time.time()
    while d <= end:
        try:
            total += process_day(d, abbr_to_id)
        except Exception:
            log.exception("  [%s] %s 失敗，略過（重跑可補齊）", season, d)
            failed.append(d.isoformat())
        i += 1
        if i % 30 == 0:
            log.info("  [%s] %s，累計 %d 場，失敗 %d 天，耗時 %.0fs", season, d, total, len(failed), time.time() - t0)
        d += timedelta(days=1)
        time.sleep(delay)
    return total, failed


def main() -> None:
    from ..logging_conf import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser(description="ESPN 備援回填（賽程/比分/逐節）")
    ap.add_argument("--seasons", nargs="+", default=["2021-22", "2022-23", "2023-24", "2024-25", "2025-26"])
    args = ap.parse_args()

    from .backfill import clean_seed_demo_games, upsert_all_teams
    clean_seed_demo_games()
    upsert_all_teams()
    with cursor() as cur:
        cur.execute("SELECT abbr, id FROM teams")
        abbr_to_id = {r["abbr"]: r["id"] for r in cur.fetchall()}

    for season in args.seasons:
        log.info("=== ESPN 回填 %s ===", season)
        total, failed = backfill_season(season, abbr_to_id)
        log.info("=== %s 完成：%d 場，失敗 %d 天 %s ===", season, total, len(failed), failed[:10])
        with cursor() as cur:
            heartbeat(cur, source_key="espn", display_name="ESPN API", category="games",
                      status="ok" if not failed else "warn",
                      error=f"{len(failed)} 天失敗" if failed else None,
                      records_updated=total, expected_interval_min=60 * 24)


if __name__ == "__main__":
    main()
