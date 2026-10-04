"""
Production 輸入載入（DB → HistoryInputs / ScheduledGame）
------------------------------------------------------------
所有查詢都以「預測時間戳 as_of」為界：
  * 歷史：status='final'、有比分、開賽 < as_of 的比賽（及其 box score 衍生指標、球員分鐘）
  * 傷病快照：report_time_utc < as_of（team_state_asof 本身也只取嚴格早於 cutoff 的報告）
  * 未開賽：status <> 'final' 且開賽在 (as_of, as_of + horizon]
  * pending：status <> 'final' 且開賽在 as_of 前 8 天 ~ 最晚一場候選比賽之間（只用開賽時間；結果未知）
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .. import metrics
from ..injury_asof import InjuryIndex, load_index
from ..models.pregame_features import TEAM_METRICS, GameRecord
from ..timeutil import ensure_utc
from .features import HISTORY_STAGES, HistoryInputs, ScheduledGame

INJURY_LOOKBACK = timedelta(days=5)        # ≥ injury_asof.LOOKBACK_DAYS（4 天）


def load_history(cur, as_of: datetime, *, injury_since: datetime | None = None, full_injuries: bool = False
                 ) -> HistoryInputs:
    as_of = ensure_utc(as_of)
    cur.execute(
        """
        SELECT g.id, g.season, g.season_stage, g.date_utc, g.home_team_id, g.away_team_id,
               ht.abbr AS h_abbr, at.abbr AS a_abbr, g.home_pts, g.away_pts, g.home_h1, g.away_h1
          FROM games g JOIN teams ht ON ht.id = g.home_team_id JOIN teams at ON at.id = g.away_team_id
         WHERE g.status = 'final' AND g.season_stage = ANY(%s)
           AND g.home_pts IS NOT NULL AND g.away_pts IS NOT NULL AND g.date_utc < %s
         ORDER BY g.date_utc, g.id""", (list(HISTORY_STAGES), as_of))
    rows = cur.fetchall()
    games = [GameRecord(r["id"], r["season"], ensure_utc(r["date_utc"]), r["home_team_id"], r["away_team_id"],
                        r["h_abbr"], r["a_abbr"], r["home_pts"], r["away_pts"], r["home_h1"], r["away_h1"])
             for r in rows]
    stages = {r["id"]: r["season_stage"] for r in rows}
    cur.execute(
        "SELECT d.game_id, d.team_id, " + ", ".join(f"d.{m}" for m in TEAM_METRICS)
        + " FROM team_game_derived d JOIN games g ON g.id = d.game_id"
          " WHERE d.formula_version = %s AND g.status = 'final' AND g.date_utc < %s",
        (metrics.FORMULA_VERSION, as_of))
    derived = {(r["game_id"], r["team_id"]): {m: r[m] for m in TEAM_METRICS} for r in cur.fetchall()}
    cur.execute(
        """SELECT s.game_id, s.team_id, s.player_id, s.min, s.started, s.plus_minus
             FROM player_game_stats s JOIN games g ON g.id = s.game_id
            WHERE g.status = 'final' AND g.date_utc < %s""", (as_of,))
    players: dict[int, list] = defaultdict(list)
    for r in cur.fetchall():
        players[r["game_id"]].append((r["team_id"], r["player_id"], float(r["min"] or 0.0), bool(r["started"]),
                                      float(r["plus_minus"] or 0.0)))
    if full_injuries:
        index = load_index(cur)
    else:
        index = load_index(cur, since=injury_since or (as_of - INJURY_LOOKBACK))
    index = InjuryIndex([s for s in index.snapshots if s.report_time_utc < as_of])
    return HistoryInputs(games, stages, derived, dict(players), index, as_of)


def _scheduled(rows) -> list[ScheduledGame]:
    return [ScheduledGame(r["id"], r["season"], r["season_stage"] or "regular", ensure_utc(r["date_utc"]),
                          r["home_team_id"], r["away_team_id"], r["h_abbr"], r["a_abbr"], r["nba_game_id"],
                          r["status"]) for r in rows]


_SCHED_SQL = """
    SELECT g.id, g.nba_game_id, g.season, g.season_stage, g.date_utc, g.status, g.home_team_id, g.away_team_id,
           ht.abbr AS h_abbr, at.abbr AS a_abbr
      FROM games g JOIN teams ht ON ht.id = g.home_team_id JOIN teams at ON at.id = g.away_team_id
     WHERE g.status <> 'final' AND g.season_stage = ANY(%s) AND g.date_utc > %s AND g.date_utc <= %s
     ORDER BY g.date_utc, g.id"""


def load_upcoming(cur, as_of: datetime, until: datetime, *, game_ids: list[int] | None = None) -> list[ScheduledGame]:
    cur.execute(_SCHED_SQL, (list(HISTORY_STAGES), ensure_utc(as_of), ensure_utc(until)))
    out = _scheduled(cur.fetchall())
    return [g for g in out if game_ids is None or g.game_id in set(game_ids)]


def load_pending(cur, as_of: datetime, until: datetime) -> list[ScheduledGame]:
    """尚未 final 的比賽（含進行中、應已結束但結果未同步者），只用其開賽時間。"""
    cur.execute(
        """
        SELECT g.id, g.nba_game_id, g.season, g.season_stage, g.date_utc, g.status, g.home_team_id, g.away_team_id,
               ht.abbr AS h_abbr, at.abbr AS a_abbr
          FROM games g JOIN teams ht ON ht.id = g.home_team_id JOIN teams at ON at.id = g.away_team_id
         WHERE g.status <> 'final' AND g.season_stage = ANY(%s)
           AND g.date_utc >= %s AND g.date_utc < %s
         ORDER BY g.date_utc, g.id""", (list(HISTORY_STAGES), ensure_utc(as_of) - timedelta(days=8), ensure_utc(until)))
    return _scheduled(cur.fetchall())


def final_games_missing_scores(cur, as_of: datetime) -> list[int]:
    """status=final 但比分缺漏（不會進入歷史狀態；資料品質用）。"""
    cur.execute("SELECT id FROM games WHERE status = 'final' AND (home_pts IS NULL OR away_pts IS NULL)"
                " AND date_utc < %s AND date_utc >= %s", (ensure_utc(as_of), ensure_utc(as_of) - timedelta(days=30)))
    return [r["id"] for r in cur.fetchall()]
