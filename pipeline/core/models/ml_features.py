"""
Phase C 比賽層級特徵（只用比賽開打「前」已知的資訊）
------------------------------------------------------------
所有滾動統計一律先 shift(1) 再計算，確保不含當場結果。
來源只有 games / elo_ratings（暫無 box score 與傷病資料，見 README）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ELO_MODEL_VERSION = "elo-v1.0"

TARGETS = ["win", "margin", "total", "h1_margin", "h1_total"]


def load_games(cur) -> pd.DataFrame:
    cur.execute(
        """
        SELECT g.id AS game_id, g.season, g.season_stage, g.date_utc,
               g.home_team_id, g.away_team_id, g.home_pts, g.away_pts,
               g.home_h1, g.away_h1,
               eh.rating_before AS elo_home, ea.rating_before AS elo_away
          FROM games g
          LEFT JOIN elo_ratings eh ON eh.game_id = g.id AND eh.team_id = g.home_team_id
                                   AND eh.model_version = %s
          LEFT JOIN elo_ratings ea ON ea.game_id = g.id AND ea.team_id = g.away_team_id
                                   AND ea.model_version = %s
         WHERE g.nba_game_id IS NOT NULL AND g.status = 'final'
           AND g.home_pts IS NOT NULL AND g.away_pts IS NOT NULL
           AND g.season_stage IN ('regular', 'playoffs')
         ORDER BY g.date_utc, g.id
        """,
        (ELO_MODEL_VERSION, ELO_MODEL_VERSION),
    )
    df = pd.DataFrame(cur.fetchall())
    df["date_utc"] = pd.to_datetime(df["date_utc"], utc=True)
    for c in ("home_pts", "away_pts", "home_h1", "away_h1", "elo_home", "elo_away"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _team_long(df: pd.DataFrame) -> pd.DataFrame:
    common = ["game_id", "season", "date_utc"]
    home = df[common].copy()
    home["team_id"], home["is_home"] = df["home_team_id"], 1
    home["pf"], home["pa"] = df["home_pts"], df["away_pts"]
    home["h1f"], home["h1a"] = df["home_h1"], df["away_h1"]
    away = df[common].copy()
    away["team_id"], away["is_home"] = df["away_team_id"], 0
    away["pf"], away["pa"] = df["away_pts"], df["home_pts"]
    away["h1f"], away["h1a"] = df["away_h1"], df["home_h1"]
    long = pd.concat([home, away], ignore_index=True)
    long["win"] = (long["pf"] > long["pa"]).astype(float)
    long["margin"] = long["pf"] - long["pa"]
    return long.sort_values(["team_id", "date_utc", "game_id"]).reset_index(drop=True)


def _roll(g: pd.core.groupby.SeriesGroupBy, n: int, minp: int = 3) -> pd.Series:
    return g.transform(lambda s: s.shift(1).rolling(n, min_periods=minp).mean())


def _team_features(long: pd.DataFrame) -> pd.DataFrame:
    gb = long.groupby("team_id", sort=False)
    out = long[["game_id", "team_id"]].copy()
    gap = gb["date_utc"].diff().dt.total_seconds() / 86400.0
    out["rest_days"] = (gap - 1).clip(lower=0, upper=7)  # 首場為 NaN
    out["b2b"] = (out["rest_days"] == 0).astype(float).where(out["rest_days"].notna())
    out["form5"] = _roll(gb["win"], 5)
    out["form10"] = _roll(gb["win"], 10)
    out["margin10"] = _roll(gb["margin"], 10)
    out["pf10"] = _roll(gb["pf"], 10)
    out["pa10"] = _roll(gb["pa"], 10)
    out["h1f10"] = _roll(gb["h1f"], 10)
    out["h1a10"] = _roll(gb["h1a"], 10)
    # 近 7 天賽程密度（含當天之前的場次數）
    def games_last7(s: pd.Series) -> pd.Series:
        t = s.values.astype("datetime64[s]").astype("int64")
        res = np.zeros(len(t))
        j = 0
        for i in range(len(t)):
            while t[i] - t[j] > 7 * 86400:
                j += 1
            res[i] = i - j  # 不含當場
        return pd.Series(res, index=s.index)

    out["games_7d"] = gb["date_utc"].transform(games_last7)
    # 賽季第幾場（賽季進度）
    out["season_game_no"] = long.groupby(["team_id", "season"]).cumcount()
    return out


def _h1_pairs(df: pd.DataFrame) -> pd.Series:
    """主隊視角：近 5 次交手（含季後賽）主隊勝率，不含當場。"""
    lo = df[["home_team_id", "away_team_id"]].min(axis=1)
    hi = df[["home_team_id", "away_team_id"]].max(axis=1)
    key = lo.astype(str) + "-" + hi.astype(str)
    lo_won = np.where(df["home_team_id"] == lo, df["home_pts"] > df["away_pts"],
                      df["away_pts"] > df["home_pts"]).astype(float)
    s = pd.Series(lo_won, index=df.index)
    rolled = s.groupby(key).transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    return pd.Series(np.where(df["home_team_id"] == lo, rolled, 1 - rolled), index=df.index)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["date_utc", "game_id"]).reset_index(drop=True)
    tf = _team_features(_team_long(df))
    feats = df[["game_id", "season", "season_stage", "date_utc", "home_team_id", "away_team_id"]].copy()

    for side, tid in (("home", "home_team_id"), ("away", "away_team_id")):
        m = tf.rename(columns={c: f"{side}_{c}" for c in tf.columns if c not in ("game_id", "team_id")})
        feats = feats.merge(m, left_on=["game_id", tid], right_on=["game_id", "team_id"], how="left") \
                     .drop(columns="team_id")

    feats["elo_home"], feats["elo_away"] = df["elo_home"].values, df["elo_away"].values
    feats["elo_diff"] = feats["elo_home"] - feats["elo_away"]
    feats["form10_diff"] = feats["home_form10"] - feats["away_form10"]
    feats["margin10_diff"] = feats["home_margin10"] - feats["away_margin10"]
    feats["rest_diff"] = feats["home_rest_days"] - feats["away_rest_days"]
    feats["b2b_diff"] = feats["home_b2b"].fillna(0) - feats["away_b2b"].fillna(0)
    # 預期總分/半場總分的粗估（雙方近期攻守平均）
    feats["total_est"] = (feats["home_pf10"] + feats["home_pa10"] + feats["away_pf10"] + feats["away_pa10"]) / 2
    feats["h1_total_est"] = (feats["home_h1f10"] + feats["home_h1a10"] + feats["away_h1f10"] + feats["away_h1a10"]) / 2
    feats["h2h_home_win5"] = _h1_pairs(df).values
    feats["is_playoffs"] = (df["season_stage"] == "playoffs").astype(int).values

    feats["win"] = (df["home_pts"] > df["away_pts"]).astype(int).values
    feats["margin"] = (df["home_pts"] - df["away_pts"]).values
    feats["total"] = (df["home_pts"] + df["away_pts"]).values
    feats["h1_margin"] = (df["home_h1"] - df["away_h1"]).values
    feats["h1_total"] = (df["home_h1"] + df["away_h1"]).values
    return feats


FEATURE_COLUMNS = [
    "elo_home", "elo_away", "elo_diff",
    "home_form5", "home_form10", "away_form5", "away_form10", "form10_diff",
    "home_margin10", "away_margin10", "margin10_diff",
    "home_pf10", "home_pa10", "away_pf10", "away_pa10",
    "home_h1f10", "home_h1a10", "away_h1f10", "away_h1a10",
    "home_rest_days", "away_rest_days", "rest_diff", "home_b2b", "away_b2b",
    "home_games_7d", "away_games_7d",
    "home_season_game_no", "away_season_game_no",
    "total_est", "h1_total_est", "h2h_home_win5", "is_playoffs",
]

# 勝負模型（邏輯迴歸）使用的特徵：以評測賽季「之前」的賽季做前向選擇得出（2022-23、2023-24 驗證）
WIN_FEATURES = ["elo_diff", "b2b_diff", "margin10_diff", "rest_diff", "form10_diff"]

# 前端「特徵拆解」使用的中文標籤
FEATURE_LABELS = {
    "elo_home": "主隊 Elo", "elo_away": "客隊 Elo", "elo_diff": "Elo 差距",
    "home_form5": "主隊近5場勝率", "home_form10": "主隊近10場勝率",
    "away_form5": "客隊近5場勝率", "away_form10": "客隊近10場勝率", "form10_diff": "近10場勝率差",
    "home_margin10": "主隊近10場平均分差", "away_margin10": "客隊近10場平均分差",
    "margin10_diff": "近10場分差差距",
    "home_pf10": "主隊近10場得分", "home_pa10": "主隊近10場失分",
    "away_pf10": "客隊近10場得分", "away_pa10": "客隊近10場失分",
    "home_h1f10": "主隊近10場上半場得分", "home_h1a10": "主隊近10場上半場失分",
    "away_h1f10": "客隊近10場上半場得分", "away_h1a10": "客隊近10場上半場失分",
    "home_rest_days": "主隊休息天數", "away_rest_days": "客隊休息天數", "rest_diff": "休息天數差",
    "home_b2b": "主隊背靠背", "away_b2b": "客隊背靠背",
    "home_games_7d": "主隊近7天場次", "away_games_7d": "客隊近7天場次",
    "home_season_game_no": "主隊賽季出賽數", "away_season_game_no": "客隊賽季出賽數",
    "total_est": "雙方攻守總分估計", "h1_total_est": "雙方上半場總分估計",
    "h2h_home_win5": "近5次交手主隊勝率", "is_playoffs": "季後賽",
    "b2b_diff": "背靠背差（主-客）",
}
