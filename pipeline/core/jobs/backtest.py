"""
Phase B-8/9：Elo walk-forward 回測 + 寫入預測
------------------------------------------------------------
嚴禁隨機切分造成資料洩漏：一律按比賽時間嚴格排序處理，任何一場比賽的
預測只能用「嚴格早於該場比賽時間」已經處理過的資料（先前比賽結果、
先前已知的傷病申報、先前的先發名單）。

流程：
  1. 用前 3 季（若有）當「暖身」，把 Elo 評分帶到合理水準，不計入評測。
  2. 最近 2 季的每一場比賽：先用當前狀態算預測機率 → 記錄評測用資料 →
     再用實際結果更新 Elo。暖身季一樣照走完整流程（預測→更新），只是
     不計入 accuracy／不寫入 predictions。
  3. 回測結束後，把最近 2 季逐場預測寫入 predictions，彙總指標寫入
     model_metrics。
"""
from __future__ import annotations

import argparse
import logging
import math
from bisect import bisect_right
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

from ..config import settings
from ..db import cursor
from ..models import elo, features

log = logging.getLogger(__name__)

MAX_NORMAL_GAP_DAYS = 10  # 超過此天數視為季初/明星賽假期，不做休息天數調整
FORM_WINDOW = 10


def _load_games() -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT id, nba_game_id, season, season_stage, date_utc,
                   home_team_id, away_team_id, home_pts, away_pts
              FROM games
             WHERE nba_game_id IS NOT NULL AND status = 'final'
               AND home_pts IS NOT NULL AND away_pts IS NOT NULL
             ORDER BY date_utc ASC
            """
        )
        return cur.fetchall()


def _load_player_starts() -> dict[int, list[tuple[datetime, int, int, bool]]]:
    """依隊伍分組、依時間排序的 (date_utc, game_id, player_id, started)"""
    with cursor() as cur:
        cur.execute(
            """
            SELECT g.date_utc, pgs.game_id, pgs.team_id, pgs.player_id, pgs.started
              FROM player_game_stats pgs
              JOIN games g ON g.id = pgs.game_id
             WHERE g.nba_game_id IS NOT NULL AND g.status = 'final'
             ORDER BY g.date_utc ASC
            """
        )
        rows = cur.fetchall()
    by_team: dict[int, list] = defaultdict(list)
    for r in rows:
        by_team[r["team_id"]].append(r)
    return by_team


def _load_injury_timelines() -> dict[int, list[tuple[datetime, str]]]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT player_id, report_time_utc, status
              FROM injuries
             WHERE player_id IS NOT NULL
             ORDER BY report_time_utc ASC
            """
        )
        rows = cur.fetchall()
    timelines: dict[int, list[tuple[datetime, str]]] = defaultdict(list)
    for r in rows:
        timelines[r["player_id"]].append((r["report_time_utc"], r["status"]))
    return timelines


def _status_as_of(timeline: list[tuple[datetime, str]], as_of: datetime) -> str | None:
    times = [t for t, _ in timeline]
    i = bisect_right(times, as_of) - 1
    return timeline[i][1] if i >= 0 else None


OUT_STATUSES = {"Out", "Doubtful"}


def run_backtest(eval_seasons_count: int = 2) -> dict[str, Any]:
    games = _load_games()
    if not games:
        log.warning("games 表沒有已完成的真實比賽，無法回測")
        return {}

    seasons_in_order = sorted({g["season"] for g in games})
    eval_seasons = set(seasons_in_order[-eval_seasons_count:])
    log.info("載入 %d 場比賽，涵蓋賽季 %s；評測賽季：%s",
             len(games), seasons_in_order, sorted(eval_seasons))

    player_starts_by_team = _load_player_starts()
    injury_timelines = _load_injury_timelines()

    # per-team recent starters：走到某場比賽「之前」最近一場該隊的先發名單
    recent_starters: dict[int, set[int]] = {}
    starts_cursor: dict[int, int] = defaultdict(int)  # 指到 player_starts_by_team 已處理到哪

    def advance_starters(team_id: int, before: datetime) -> None:
        rows = player_starts_by_team.get(team_id, [])
        i = starts_cursor[team_id]
        last_game_id = None
        starters_this_game: set[int] = set()
        while i < len(rows) and rows[i]["date_utc"] < before:
            r = rows[i]
            if last_game_id is not None and r["game_id"] != last_game_id:
                recent_starters[team_id] = starters_this_game
                starters_this_game = set()
            if r["started"]:
                starters_this_game.add(r["player_id"])
            last_game_id = r["game_id"]
            i += 1
        if starters_this_game:
            recent_starters[team_id] = starters_this_game
        starts_cursor[team_id] = i

    ratings: dict[int, float] = defaultdict(lambda: elo.INITIAL_RATING)
    last_season: dict[int, str] = {}
    last_game_date: dict[int, datetime] = {}
    form_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=FORM_WINDOW))

    eval_rows: list[dict] = []  # 供 predictions 寫入 + 指標計算

    for g in games:
        gdate: datetime = g["date_utc"]
        home_id, away_id = g["home_team_id"], g["away_team_id"]

        # 新賽季 → 迴歸平均
        for tid in (home_id, away_id):
            if last_season.get(tid) and last_season[tid] != g["season"]:
                ratings[tid] = elo.regress_to_mean(ratings[tid])
            last_season[tid] = g["season"]

        # 休息天數（None = 季初/明星賽假期等異常長間隔，不調整）
        def rest_days(tid: int) -> int | None:
            prev = last_game_date.get(tid)
            if prev is None:
                return None
            gap = (gdate - prev).days - 1
            return gap if 0 <= gap <= MAX_NORMAL_GAP_DAYS else None

        home_rest, away_rest = rest_days(home_id), rest_days(away_id)

        # 先發缺陣人數（用「這場之前」已知的最新傷病狀態 × 這場之前最近一次的先發名單）
        advance_starters(home_id, gdate)
        advance_starters(away_id, gdate)
        home_out = sum(
            1 for pid in recent_starters.get(home_id, set())
            if _status_as_of(injury_timelines.get(pid, []), gdate) in OUT_STATUSES
        )
        away_out = sum(
            1 for pid in recent_starters.get(away_id, set())
            if _status_as_of(injury_timelines.get(pid, []), gdate) in OUT_STATUSES
        )

        home_rating, away_rating = ratings[home_id], ratings[away_id]
        base_diff = (home_rating + elo.HOME_ADVANTAGE) - away_rating
        rest_adj = features.rest_adjustment(home_rest, away_rest)
        home_pen, away_pen, injury_adj = features.injury_adjustment(home_out, away_out)
        adjusted_diff = base_diff + rest_adj + injury_adj
        prob_home = elo.win_prob(adjusted_diff)

        home_won = g["home_pts"] > g["away_pts"]
        margin = g["home_pts"] - g["away_pts"]

        if g["season"] in eval_seasons:
            eval_rows.append({
                "game_id": g["id"], "season": g["season"], "prob_home": prob_home,
                "home_won": home_won,
                "features_json": {
                    "elo_home": round(home_rating, 1), "elo_away": round(away_rating, 1),
                    "home_advantage": elo.HOME_ADVANTAGE,
                    "rest_days_home": home_rest, "rest_days_away": away_rest,
                    "starters_out_home": home_out, "starters_out_away": away_out,
                    "contributions": [
                        {"label": "Elo 基礎差距", "value": round(home_rating - away_rating, 1)},
                        {"label": "主場優勢 (Elo)", "value": elo.HOME_ADVANTAGE},
                        {"label": "休息天數調整 (Elo)", "value": round(rest_adj, 1)},
                        {"label": "傷病影響 (Elo)", "value": round(injury_adj, 1)},
                    ],
                },
            })

        # 用「未調整臨場因素」的基礎評分差更新 Elo，評分只反映球隊實力
        new_home, new_away = elo.update_ratings(
            home_rating, away_rating, home_won=home_won, margin=margin, rating_diff=base_diff,
        )
        ratings[home_id], ratings[away_id] = new_home, new_away
        last_game_date[home_id] = gdate
        last_game_date[away_id] = gdate
        form_history[home_id].appendleft(home_won)
        form_history[away_id].appendleft(not home_won)

        _persist_elo_row(g, home_id, home_rating, new_home)
        _persist_elo_row(g, away_id, away_rating, new_away)

    metrics = _score(eval_rows)
    _persist_predictions(eval_rows)
    _persist_metrics(metrics, eval_seasons, seasons_in_order)
    return metrics


def _persist_elo_row(g: dict, team_id: int, before: float, after: float) -> None:
    opp = g["away_team_id"] if team_id == g["home_team_id"] else g["home_team_id"]
    is_home = 1 if team_id == g["home_team_id"] else 0
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO elo_ratings
              (team_id, game_id, season, game_date_utc, opponent_team_id, is_home,
               rating_before, rating_after, model_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (team_id, game_id, model_version) DO UPDATE SET
              rating_before = EXCLUDED.rating_before, rating_after = EXCLUDED.rating_after
            """,
            (team_id, g["id"], g["season"], g["date_utc"], opp, is_home,
             before, after, settings.model_version),
        )


def _score(eval_rows: list[dict]) -> dict[str, Any]:
    n = len(eval_rows)
    if n == 0:
        return {}
    correct = 0
    log_loss_sum = 0.0
    brier_sum = 0.0
    eps = 1e-9
    for r in eval_rows:
        p = min(max(r["prob_home"], eps), 1 - eps)
        actual = 1.0 if r["home_won"] else 0.0
        predicted_home = p > 0.5
        if predicted_home == r["home_won"]:
            correct += 1
        log_loss_sum += -(actual * math.log(p) + (1 - actual) * math.log(1 - p))
        brier_sum += (p - actual) ** 2
    return {
        "n_games": n,
        "accuracy": correct / n,
        "log_loss": log_loss_sum / n,
        "brier": brier_sum / n,
    }


def _persist_predictions(eval_rows: list[dict]) -> None:
    with cursor() as cur:
        for r in eval_rows:
            confidence = round(abs(r["prob_home"] - 0.5) * 2, 4)
            cur.execute(
                """
                INSERT INTO predictions
                  (game_id, model_version, created_at, home_win_prob, confidence, features_json)
                VALUES (%s, %s, NOW(), %s, %s, %s::jsonb)
                """,
                (r["game_id"], settings.model_version, r["prob_home"], confidence,
                 _to_json(r["features_json"])),
            )
    log.info("已寫入 %d 筆 predictions（model_version=%s）", len(eval_rows), settings.model_version)


def _to_json(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def _persist_metrics(metrics: dict, eval_seasons: set[str], all_seasons: list[str]) -> None:
    if not metrics:
        return
    warm_up = [s for s in all_seasons if s not in eval_seasons]
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO model_metrics
              (model_version, season, evaluated_at, n_games, accuracy, log_loss, brier, notes)
            VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s)
            ON CONFLICT (model_version, season, evaluated_at) DO NOTHING
            """,
            (
                settings.model_version, ",".join(sorted(eval_seasons)), metrics["n_games"],
                metrics["accuracy"], metrics["log_loss"], metrics["brier"],
                f"walk-forward；暖身賽季（不計入指標）：{warm_up or '無'}",
            ),
        )
    log.info("回測結果：n=%d accuracy=%.4f log_loss=%.4f brier=%.4f",
             metrics["n_games"], metrics["accuracy"], metrics["log_loss"], metrics["brier"])


def main() -> None:
    from ..logging_conf import setup_logging
    setup_logging()
    parser = argparse.ArgumentParser(description="Elo walk-forward 回測")
    parser.add_argument("--eval-seasons", type=int, default=2)
    args = parser.parse_args()
    run_backtest(args.eval_seasons)


if __name__ == "__main__":
    main()
