"""
Phase C.5C：評測輸入的本機快照
------------------------------------------------------------
C.5C 要對同一批歷史資料建多個決策時點的特徵、跑多組 walk-forward 實驗；每次都從 Supabase 拉
90 萬列傷病快照太慢，所以一次載入後存成 pipeline/artifacts/c5c_inputs.pkl（gitignore）。

內容全部是「原始輸入」（賽事、box-v1 指標、球員分鐘/先發/正負值、傷病快照索引、Elo 賽前評分），
不含任何由結果擬合出的東西；特徵與模型都在之後依時間順序計算。
"""
from __future__ import annotations

import logging
import pickle
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ..injury_asof import InjuryIndex, load_index
from ..models import pregame_features as pf
from ..models.ml_features import ELO_MODEL_VERSION, build_features, load_games

log = logging.getLogger(__name__)

CACHE = Path(__file__).resolve().parents[2] / "artifacts" / "c5c_inputs.pkl"


@dataclass
class C5CInputs:
    games: list[pf.GameRecord]
    derived: dict[tuple[int, int], dict]
    # {game_id: [(team_id, player_id, minutes, started, plus_minus)]}——只有實際上場（min > 0）的球員
    players: dict[int, list[tuple[int, int, float, bool, float]]]
    injury_index: InjuryIndex
    elo: pd.DataFrame            # game_id, elo_home, elo_away（elo-v1.0 賽前評分）
    elo_pred: pd.DataFrame       # game_id, elo_p（elo-v1.0 已寫入 predictions 的官方 baseline，僅評測賽季）
    phase_c: pd.DataFrame        # 既有 Phase C 比賽層級特徵（ml_features.build_features）


def load_from_db(cur) -> C5CInputs:
    games, derived, _ = pf.load_inputs(cur)
    cur.execute("SELECT game_id, team_id, player_id, min, started, plus_minus FROM player_game_stats")
    players: dict[int, list] = defaultdict(list)
    for r in cur.fetchall():
        players[r["game_id"]].append((r["team_id"], r["player_id"], float(r["min"] or 0.0), bool(r["started"]),
                                      float(r["plus_minus"] or 0.0)))
    index = load_index(cur)
    cur.execute(
        """SELECT g.id AS game_id, eh.rating_before AS elo_home, ea.rating_before AS elo_away
             FROM games g
             JOIN elo_ratings eh ON eh.game_id = g.id AND eh.team_id = g.home_team_id AND eh.model_version = %s
             JOIN elo_ratings ea ON ea.game_id = g.id AND ea.team_id = g.away_team_id AND ea.model_version = %s""",
        (ELO_MODEL_VERSION, ELO_MODEL_VERSION))
    elo = pd.DataFrame(cur.fetchall()).astype(float)
    elo["game_id"] = elo["game_id"].astype(int)
    cur.execute("SELECT game_id, home_win_prob AS elo_p FROM predictions WHERE model_version = %s",
                (ELO_MODEL_VERSION,))
    elo_pred = pd.DataFrame(cur.fetchall())
    elo_pred["elo_p"] = elo_pred["elo_p"].astype(float)
    phase_c = build_features(load_games(cur))
    return C5CInputs(games, derived, dict(players), index, elo, elo_pred, phase_c)


def load(refresh: bool = False) -> C5CInputs:
    if CACHE.exists() and not refresh:
        with CACHE.open("rb") as f:
            return pickle.load(f)
    from ..db import cursor
    with cursor() as cur:
        data = load_from_db(cur)
    CACHE.parent.mkdir(exist_ok=True)
    with CACHE.open("wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    log.info("C.5C 輸入快照：%d 場、%d 份傷病報告 → %s", len(data.games), len(data.injury_index.snapshots), CACHE)
    return data
