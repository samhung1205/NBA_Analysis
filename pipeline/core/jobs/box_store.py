"""
基本 box score 寫入（賽後結算與歷史回填共用）
------------------------------------------------------------
一場比賽的球隊/球員統計、逐節/半場、衍生進階指標在**同一個交易**內寫入：
要嘛整場完整，要嘛完全沒寫（被中斷時不留下半套資料，重跑會自動補）。
"""
from __future__ import annotations

from typing import Any

from .. import metrics
from ..db import GAME_COLUMNS, TEAM_STATS_COLUMNS, upsert_team_derived

PLAYER_FIELDS = ("min", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga",
                 "fg3m", "fg3a", "ftm", "fta", "plus_minus", "started")


def _multi_insert(cur, table: str, cols: list[str], rows: list[list[Any]], conflict: str,
                  update_cols: list[str], returning: str | None = None) -> list[dict]:
    """單一語句多列 UPSERT：遠端資料庫每個語句一次往返，逐列寫入一場比賽要 ~40 次往返。"""
    if not rows:
        return []
    ph = "(" + ", ".join(["%s"] * len(cols)) + ")"
    update = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols) if update_cols else None
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES {', '.join([ph] * len(rows))} "
           f"ON CONFLICT ({conflict}) " + (f"DO UPDATE SET {update}" if update else "DO NOTHING")
           + (f" RETURNING {returning}" if returning else ""))
    cur.execute(sql, [v for r in rows for v in r])
    return cur.fetchall() if returning else []


def store_box_basic(cur, game: dict[str, Any], box: dict[str, Any], *, keep_player_team: bool = False) -> None:
    """game: {id, home_team_id, away_team_id}（本地 games 列）；box: nba_cdn.fetch_box_basic / nba_stats 形狀。

    keep_player_team=True（歷史回填）：不覆寫既有球員的 team_id / is_starter（回填可能亂序，
    目前球隊由 db.refresh_player_current_team 依最近出賽統一回填）。
    """
    sides = (("home", game["home_team_id"]), ("away", game["away_team_id"]))

    team_cols = ["game_id", "team_id", "is_home"] + [c for c in TEAM_STATS_COLUMNS if c != "is_home"
                                                      and c in box["home"]["team"]]
    _multi_insert(
        cur, "team_game_stats", team_cols,
        [[game["id"], tid, 1 if side == "home" else 0] + [box[side]["team"][c] for c in team_cols[3:]]
         for side, tid in sides],
        conflict="game_id, team_id", update_cols=team_cols[2:])

    players: dict[int, dict] = {}
    for side, tid in sides:
        for p in box[side]["players"]:
            players[p["nba_player_id"]] = {**p, "team_id": tid}
    if players:
        # 依 nba_player_id 排序：多個回填執行緒同時寫入（季後賽球員重疊）時鎖定順序一致，避免死結
        plist = sorted(players.values(), key=lambda p: p["nba_player_id"])
        if keep_player_team:
            ids = _multi_insert(
                cur, "players", ["nba_player_id", "name", "team_id", "position"],
                [[p["nba_player_id"], p["name"], p["team_id"], p["position"]] for p in plist],
                conflict="nba_player_id", update_cols=[], returning="id, nba_player_id")
            # 既有球員只更新姓名/位置，不動 team_id / is_starter
            cur.execute(
                "UPDATE players SET name = v.name, position = COALESCE(v.position, players.position)"
                " FROM (SELECT unnest(%s::int[]) AS nid, unnest(%s::text[]) AS name, unnest(%s::text[]) AS position) v"
                " WHERE players.nba_player_id = v.nid"
                "   AND (players.name IS DISTINCT FROM v.name"
                "        OR players.position IS DISTINCT FROM COALESCE(v.position, players.position))",
                ([p["nba_player_id"] for p in plist], [p["name"] for p in plist], [p["position"] for p in plist]))
            if len(ids) < len(plist):  # DO NOTHING 不回傳既有列：另查
                cur.execute("SELECT id, nba_player_id FROM players WHERE nba_player_id = ANY(%s)",
                            ([p["nba_player_id"] for p in plist],))
                ids = cur.fetchall()
        else:
            ids = _multi_insert(
                cur, "players", ["nba_player_id", "name", "team_id", "position", "is_starter"],
                [[p["nba_player_id"], p["name"], p["team_id"], p["position"], p["started"]] for p in plist],
                conflict="nba_player_id", update_cols=["name", "team_id", "position", "is_starter"],
                returning="id, nba_player_id")
        local = {r["nba_player_id"]: r["id"] for r in ids}
        pcols = ["game_id", "player_id", "team_id"] + [c for c in PLAYER_FIELDS]
        _multi_insert(
            cur, "player_game_stats", pcols,
            [[game["id"], local[p["nba_player_id"]], p["team_id"]] + [p.get(c) for c in PLAYER_FIELDS]
             for p in plist],
            conflict="game_id, player_id", update_cols=pcols[2:])

    if box.get("quarters"):
        q = {k: v for k, v in box["quarters"].items() if k in GAME_COLUMNS}
        sets = ", ".join(f"{k} = %s" for k in q)
        cur.execute(f"UPDATE games SET {sets}, updated_at = NOW() WHERE id = %s", [*q.values(), game["id"]])
    store_derived(cur, game, box["home"]["team"], box["away"]["team"])


def store_derived(cur, game: dict[str, Any], home_raw: dict, away_raw: dict) -> bool:
    """由雙方原始計數算衍生指標並寫入 team_game_derived；原始計數不完整 → 不寫（不補造）。"""
    if not (metrics.has_raw(home_raw) and metrics.has_raw(away_raw)):
        return False
    h, a = metrics.derive_game(home_raw, away_raw)
    upsert_team_derived(cur, game_id=game["id"], team_id=game["home_team_id"],
                        formula_version=metrics.FORMULA_VERSION, **h)
    upsert_team_derived(cur, game_id=game["id"], team_id=game["away_team_id"],
                        formula_version=metrics.FORMULA_VERSION, **a)
    return True
