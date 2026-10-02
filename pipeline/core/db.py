"""
資料庫存取層
------------------------------------------------------------
與階段一（Cloudflare Workers）連同一個 Postgres，但這裡是一般常駐 Python
行程，沒有 Workers 那種「連線不可跨請求共用」的限制，用一般連線池即可。

所有寫入採 UPSERT（以規格書 schema 既有的 UNIQUE 鍵為衝突目標），
排程可安全地重複執行、續傳，不會產生重複資料。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=4,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
    return _pool


@contextmanager
def cursor() -> Iterator[Any]:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            yield cur


# ------------------------------------------------------------------ #
# Teams / Players                                                     #
# ------------------------------------------------------------------ #

def upsert_team(cur, *, nba_team_id: int, abbr: str, name: str, name_zh: str | None = None,
                 conference: str | None = None, division: str | None = None) -> int:
    cur.execute(
        """
        INSERT INTO teams (nba_team_id, abbr, name, name_zh, conference, division)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (nba_team_id) DO UPDATE SET
          abbr = EXCLUDED.abbr, name = EXCLUDED.name,
          name_zh = COALESCE(teams.name_zh, EXCLUDED.name_zh),
          conference = EXCLUDED.conference, division = EXCLUDED.division
        RETURNING id
        """,
        (nba_team_id, abbr, name, name_zh, conference, division),
    )
    return cur.fetchone()["id"]


def upsert_player(cur, *, nba_player_id: int, name: str, team_id: int | None,
                   position: str | None = None, status: str | None = None,
                   is_starter: int = 0) -> int:
    cur.execute(
        """
        INSERT INTO players (nba_player_id, name, team_id, position, status, is_starter)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (nba_player_id) DO UPDATE SET
          name = EXCLUDED.name, team_id = EXCLUDED.team_id,
          position = EXCLUDED.position, status = EXCLUDED.status,
          is_starter = EXCLUDED.is_starter
        RETURNING id
        """,
        (nba_player_id, name, team_id, position, status, is_starter),
    )
    return cur.fetchone()["id"]


def get_team_id_by_nba_id(cur, nba_team_id: int) -> int | None:
    cur.execute("SELECT id FROM teams WHERE nba_team_id = %s", (nba_team_id,))
    row = cur.fetchone()
    return row["id"] if row else None


# ------------------------------------------------------------------ #
# Games                                                                #
# ------------------------------------------------------------------ #

GAME_COLUMNS = [
    "season", "season_stage", "date_utc", "home_team_id", "away_team_id", "arena",
    "status", "period", "home_pts", "away_pts",
    "home_q1", "home_q2", "home_q3", "home_q4", "home_ot",
    "away_q1", "away_q2", "away_q3", "away_q4", "away_ot",
    "home_h1", "home_h2", "away_h1", "away_h2",
]


def upsert_game(cur, *, nba_game_id: str, **fields: Any) -> int:
    cols = [c for c in GAME_COLUMNS if c in fields]
    placeholders = ", ".join(["%s"] * (len(cols) + 1))
    col_list = ", ".join(["nba_game_id"] + cols)
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    values = [nba_game_id] + [fields[c] for c in cols]
    cur.execute(
        f"""
        INSERT INTO games ({col_list}, updated_at)
        VALUES ({placeholders}, NOW())
        ON CONFLICT (nba_game_id) DO UPDATE SET {update_set}, updated_at = NOW()
        RETURNING id
        """,
        values,
    )
    return cur.fetchone()["id"]


def adopt_espn_game(cur, *, nba_game_id: str, home_team_id: int, away_team_id: int, date_utc: str) -> None:
    """ESPN 備援回填暫存的 'espn:<id>' 賽事，在 NBA 官方資料到手時改寫為真正的
    nba_game_id（以主客隊 + 日期±1 天認領），避免同一場比賽出現兩列。"""
    cur.execute(
        """
        UPDATE games SET nba_game_id = %s
         WHERE nba_game_id LIKE 'espn:%%'
           AND home_team_id = %s AND away_team_id = %s
           AND date_utc BETWEEN %s::timestamptz - interval '1 day' AND %s::timestamptz + interval '1 day'
           AND NOT EXISTS (SELECT 1 FROM games WHERE nba_game_id = %s)
        """,
        (nba_game_id, home_team_id, away_team_id, date_utc, date_utc, nba_game_id),
    )


def find_game_by_matchup(cur, *, home_team_id: int, away_team_id: int, date_utc) -> dict | None:
    """以主客隊 + 開賽時間（±12 小時）找既有賽事，不論其 nba_game_id 是官方或 'espn:' 暫存。
    供 ESPN 備援寫入時避免與官方來源已建立的賽事重複。"""
    cur.execute(
        """
        SELECT id, nba_game_id, status FROM games
         WHERE home_team_id = %s AND away_team_id = %s
           AND date_utc BETWEEN %s::timestamptz - interval '12 hours' AND %s::timestamptz + interval '12 hours'
         ORDER BY abs(extract(epoch FROM (date_utc - %s::timestamptz))) LIMIT 1
        """,
        (home_team_id, away_team_id, date_utc, date_utc, date_utc),
    )
    return cur.fetchone()


def get_game_id_by_nba_id(cur, nba_game_id: str) -> int | None:
    cur.execute("SELECT id FROM games WHERE nba_game_id = %s", (nba_game_id,))
    row = cur.fetchone()
    return row["id"] if row else None


# ------------------------------------------------------------------ #
# Team / Player game stats                                            #
# ------------------------------------------------------------------ #

TEAM_STATS_COLUMNS = [
    "is_home", "pts", "fg_pct", "fg3_pct", "ft_pct", "reb", "oreb", "dreb",
    "ast", "stl", "blk", "tov", "pf", "pace", "off_rtg", "def_rtg", "net_rtg",
    "ts_pct", "efg_pct", "ast_ratio", "tov_ratio",
]


def upsert_team_game_stats(cur, *, game_id: int, team_id: int, **fields: Any) -> None:
    cols = [c for c in TEAM_STATS_COLUMNS if c in fields]
    placeholders = ", ".join(["%s"] * (len(cols) + 2))
    col_list = ", ".join(["game_id", "team_id"] + cols)
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    values = [game_id, team_id] + [fields[c] for c in cols]
    cur.execute(
        f"""
        INSERT INTO team_game_stats ({col_list})
        VALUES ({placeholders})
        ON CONFLICT (game_id, team_id) DO UPDATE SET {update_set}
        """,
        values,
    )


PLAYER_STATS_COLUMNS = [
    "min", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "fg3m", "fg3a",
    "ftm", "fta", "plus_minus", "started",
]


def upsert_player_game_stats(cur, *, game_id: int, player_id: int, team_id: int | None,
                              **fields: Any) -> None:
    cols = [c for c in PLAYER_STATS_COLUMNS if c in fields]
    placeholders = ", ".join(["%s"] * (len(cols) + 3))
    col_list = ", ".join(["game_id", "player_id", "team_id"] + cols)
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    values = [game_id, player_id, team_id] + [fields[c] for c in cols]
    cur.execute(
        f"""
        INSERT INTO player_game_stats ({col_list})
        VALUES ({placeholders})
        ON CONFLICT (game_id, player_id) DO UPDATE SET {update_set}
        """,
        values,
    )


# ------------------------------------------------------------------ #
# Injuries（只在狀態變化時新增一列，保留申報歷史）                       #
# ------------------------------------------------------------------ #

def insert_injury_if_changed(cur, *, report_time_utc, player_id: int,
                              team_id: int | None, game_id: int | None,
                              status: str, reason: str | None, source: str) -> bool:
    """冪等：同一份報告（report_time_utc）重跑多少次都不會重複寫入；
    且是否「有變化」是和**該報告時間之前**的最近一筆比較（而非全表最新一筆），
    所以補抓/亂序重跑舊報告也不會產生假的狀態翻轉。回傳是否真的新增一列。"""
    cur.execute(
        """
        SELECT 1 FROM injuries
         WHERE player_id = %s AND report_time_utc = %s AND source = %s
           AND status = %s AND COALESCE(reason, '') = %s
         LIMIT 1
        """,
        (player_id, report_time_utc, source, status, reason or ""),
    )
    if cur.fetchone():
        return False
    cur.execute(
        """
        SELECT status, reason FROM injuries
         WHERE player_id = %s AND source = %s AND report_time_utc < %s
         ORDER BY report_time_utc DESC, id DESC LIMIT 1
        """,
        (player_id, source, report_time_utc),
    )
    last = cur.fetchone()
    if last and last["status"] == status and (last["reason"] or "") == (reason or ""):
        return False
    cur.execute(
        """
        INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (report_time_utc, player_id, team_id, game_id, status, reason, source),
    )
    return True


# ------------------------------------------------------------------ #
# data_sources 心跳（供 /api/system/status 顯示真實新鮮度）              #
# ------------------------------------------------------------------ #

def heartbeat(cur, *, source_key: str, display_name: str, category: str,
              status: str, error: str | None = None, records_updated: int | None = None,
              expected_interval_min: int | None = None) -> None:
    cur.execute(
        """
        INSERT INTO data_sources
          (source_key, display_name, category, last_success_at, last_attempt_at,
           last_status, last_error, records_updated, expected_interval_min)
        VALUES (%s, %s, %s, CASE WHEN %s = 'ok' THEN NOW() ELSE NULL END, NOW(),
                %s, %s, %s, %s)
        ON CONFLICT (source_key) DO UPDATE SET
          last_attempt_at = NOW(),
          last_success_at = CASE WHEN EXCLUDED.last_status = 'ok' THEN NOW()
                                  ELSE data_sources.last_success_at END,
          last_status = EXCLUDED.last_status,
          last_error = EXCLUDED.last_error,
          records_updated = COALESCE(EXCLUDED.records_updated, data_sources.records_updated),
          expected_interval_min = COALESCE(EXCLUDED.expected_interval_min, data_sources.expected_interval_min)
        """,
        (source_key, display_name, category, status, status, error, records_updated,
         expected_interval_min),
    )
