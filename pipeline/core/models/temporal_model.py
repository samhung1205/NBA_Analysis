"""
Phase C.5C：時間尺度建模（收縮 / 先驗混合、特徵組、walk-forward、評估指標）
------------------------------------------------------------
純函式庫，不碰資料庫。輸入是 temporal_features.build_temporal_features() 的輸出（+ Elo 與 Phase C 舊特徵）。

收縮 / 先驗混合（每個 walk-forward fold 只用該 fold 的訓練賽季擬合；評測賽季不參與）：

  對指標 m（例：est_net_rtg），某隊本季已賽 n 場、本季平均 x̄、上季平均 P、上季聯盟平均 L、
  名單延續性 c（ret_min_pct；本季還沒比賽時用訓練集季初平均 c̄）：

    current-only : est = L + w·(x̄ − L)                              w = n / (n + k)
    blend        : est = L + w·(x̄ − L) + (1 − w)·ρ(c)·(P − L)        ρ(c) = a + b·c
    blend+roster : 上式再加 (1 − w)·e·roster_prior_pm48（只用於分差類指標）

  k、a、b、e 以「預測該隊下一場的實際值」的平方誤差最小化擬合（k 用格點、a/b/e 用最小平方）。
  沒有上季資料（2021-22）時 prior = L。這等價於常態-常態的經驗貝氏收縮：k = 單場雜訊變異 / 隊伍間實力變異。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ..timeutil import ET
from .ml_features import WIN_FEATURES as PHASE_C_WIN_FEATURES

BLEND_METRICS = ["est_net_rtg", "est_off_rtg", "est_def_rtg", "est_pace", "margin", "pts", "opp_pts",
                 "h1_margin", "h1_pts", "h1_opp_pts"]
ROSTER_PRIOR_METRICS = {"est_net_rtg", "margin", "h1_margin"}
K_GRID = [1, 2, 3, 4, 6, 8, 10, 12, 15, 20, 25, 30, 40, 60, 100]
SIDES = ("home", "away")


# ------------------------------------------------------------------ #
# 收縮 / 先驗                                                           #
# ------------------------------------------------------------------ #

@dataclass
class BlendParams:
    metric: str
    mode: str            # current / prev_const / blend / blend_roster
    k: float
    a: float = 0.0
    b: float = 0.0
    e: float = 0.0
    c_bar: float = 0.7
    l_fallback: float = 0.0


def long_side_frame(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    """主客兩側攤平成每隊一列：gp, x̄, P, L, c, rp, y（y = 該場實際值；只在擬合時使用）。"""
    parts = []
    for side in SIDES:
        p = f"{side}_"
        part = pd.DataFrame({
            "game_id": df["game_id"].values, "season": df["season"].values, "side": side,
            "gp": df[p + "gp"].values.astype(float),
            "x": df[f"{p}{metric}_season"].values.astype(float),
            "P": df[f"{p}{metric}_prev"].values.astype(float),
            "L": df[f"league_{metric}_prev"].values.astype(float),
            "c": df[p + "ret_min_pct"].values.astype(float),
            "rp": df[p + "roster_prior_pm48"].values.astype(float),
        })
        y_col = f"y_{side}_{metric}"
        part["y"] = df[y_col].values.astype(float) if y_col in df else np.nan
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def _components(lf: pd.DataFrame, k: float, c_bar: float, l_fallback: float):
    L = lf["L"].fillna(l_fallback).values
    n = lf["gp"].values
    x = lf["x"].values
    w = np.where(n > 0, n / (n + k), 0.0)
    cur_dev = np.where(n > 0, np.nan_to_num(x - L), 0.0)
    has_p = ~np.isnan(lf["P"].values)
    pdev = np.where(has_p, lf["P"].values - L, 0.0)
    c = np.where(np.isnan(lf["c"].values), c_bar, lf["c"].values)
    rp = np.nan_to_num(lf["rp"].values)
    return L, w, cur_dev, pdev, c, rp


def predict_blend(lf: pd.DataFrame, bp: BlendParams) -> np.ndarray:
    L, w, cur_dev, pdev, c, rp = _components(lf, bp.k, bp.c_bar, bp.l_fallback)
    est = L + w * cur_dev
    if bp.mode in ("prev_const", "blend", "blend_roster"):
        est = est + (1 - w) * (bp.a + bp.b * c) * pdev
    if bp.mode == "blend_roster":
        est = est + (1 - w) * bp.e * rp
    return est


def fit_blend(train: pd.DataFrame, metric: str, mode: str) -> BlendParams:
    lf = long_side_frame(train, metric)
    lf = lf[~np.isnan(lf["y"].values)]
    l_fallback = float(lf["y"].mean())
    early = lf[(lf["gp"] >= 1) & (lf["gp"] <= 3) & lf["c"].notna()]
    c_bar = float(early["c"].mean()) if len(early) else 0.7
    if mode != "current":
        lf_fit = lf[lf["P"].notna()]          # 先驗參數只用有上季資料的列擬合
    else:
        lf_fit = lf
    best: tuple[float, BlendParams] | None = None
    for k in K_GRID:
        L, w, cur_dev, pdev, c, rp = _components(lf_fit, k, c_bar, l_fallback)
        resid = lf_fit["y"].values - (L + w * cur_dev)
        bp = BlendParams(metric, mode, float(k), c_bar=c_bar, l_fallback=l_fallback)
        if mode == "current":
            pred_r = np.zeros_like(resid)
        else:
            cols = [(1 - w) * pdev]
            if mode in ("blend", "blend_roster"):
                cols.append((1 - w) * c * pdev)
            if mode == "blend_roster":
                cols.append((1 - w) * rp)
            X = np.column_stack(cols)
            coef, *_ = np.linalg.lstsq(X, resid, rcond=None)
            pred_r = X @ coef
            bp.a = float(coef[0])
            if mode in ("blend", "blend_roster"):
                bp.b = float(coef[1])
            if mode == "blend_roster":
                bp.e = float(coef[2])
        mse = float(np.mean((resid - pred_r) ** 2))
        if best is None or mse < best[0]:
            best = (mse, bp)
    return best[1]


def fixed_weight_blend(lf: pd.DataFrame, prev_weight: float, l_fallback: float) -> np.ndarray:
    """對照組：固定權重（例：70% 上季 / 30% 本季，不隨樣本數變化）。本季無資料時全用上季。"""
    L = lf["L"].fillna(l_fallback).values
    P = np.where(np.isnan(lf["P"].values), L, lf["P"].values)
    x = np.where(lf["gp"].values > 0, np.nan_to_num(lf["x"].values, nan=0.0), P)
    x = np.where(np.isnan(lf["x"].values), P, x)
    return prev_weight * P + (1 - prev_weight) * x


def apply_blends(df: pd.DataFrame, params: dict[tuple[str, str], BlendParams]) -> pd.DataFrame:
    """把擬合好的收縮參數套到 df，產生 {side}_{m}_{mode} 欄位（mode 為 current / blend / …）。"""
    out = df.copy()
    n = len(df)
    for (metric, mode), bp in params.items():
        est = predict_blend(long_side_frame(df, metric), bp)
        out[f"home_{metric}_{mode}"] = est[:n]
        out[f"away_{metric}_{mode}"] = est[n:]
    return out


def fit_all_blends(train: pd.DataFrame, modes: Iterable[str] = ("current", "blend", "blend_roster")):
    params = {}
    for m in BLEND_METRICS:
        for mode in modes:
            if mode == "blend_roster" and m not in ROSTER_PRIOR_METRICS:
                continue
            params[(m, mode)] = fit_blend(train, m, mode)
    return params


# ------------------------------------------------------------------ #
# 模型欄位（diff / sum）與特徵組                                          #
# ------------------------------------------------------------------ #

def _d(df, col_home, col_away=None):
    return df[col_home] - df[col_away or col_home.replace("home_", "away_", 1)]


def add_model_columns(df: pd.DataFrame, fills: dict[str, float] | None = None) -> tuple[pd.DataFrame, dict]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pd.errors.PerformanceWarning)
        o, f = _add_model_columns(df, fills)
    return o.copy(), f


def _add_model_columns(df: pd.DataFrame, fills: dict[str, float] | None = None) -> tuple[pd.DataFrame, dict]:
    """由 apply_blends 後的 df 計算模型用的 diff / sum 欄位。缺值處理在這裡集中、明確：
       - 本季窗口不足（NaN）→ 以該隊 current-only 收縮值代替（「沒有近期樣本時最好的猜測」）
       - 趨勢（l10 − season）不足 → 0（沒有證據顯示有趨勢）
       - 名單延續性未知（本季第一場）→ diff 0，並有 cont_known 指示
       - 傷病未知（沒有報告涵蓋）→ diff 0，並有 inj_both_known 指示
    少數以平均值補的欄位（sum 類），平均值一律來自 fills（= 訓練集算出的常數），測試集不會影響補值。
    """
    o = df.copy()
    fills = {} if fills is None else dict(fills)

    def fill(key: str, s: pd.Series) -> float:
        if key not in fills:
            fills[key] = float(s.mean())
        return fills[key]

    for side in SIDES:
        p = f"{side}_"
        for m in ("est_net_rtg", "margin"):
            fb = o[f"{p}{m}_current"]
            for w in ("l20", "l10", "l5"):
                o[f"{p}{m}_{w}_f"] = o[f"{p}{m}_{w}"].fillna(fb)
            o[f"{p}{m}_trend10"] = (o[f"{p}{m}_l10"] - o[f"{p}{m}_season"]).fillna(0.0)
        for m in ("efg_pct", "tov_pct", "orb_pct", "opp_efg_pct", "opp_tov_pct", "est_pace"):
            league = o[f"league_{m}_prev"].fillna(fill(f"{m}_season", o[f"{p}{m}_season"]))
            o[f"{p}{m}_season_f"] = o[f"{p}{m}_season"].fillna(league)
        o[f"{p}est_pace_l10_f"] = o[f"{p}est_pace_l10"].fillna(o[f"{p}est_pace_current"])
    # ---- diff（主 − 客）---- #
    for m in ("est_net_rtg", "est_off_rtg", "est_def_rtg", "margin", "h1_margin"):
        for mode in ("current", "blend"):
            o[f"{m}_{mode}_diff"] = _d(o, f"home_{m}_{mode}")
    for m in ("est_net_rtg", "margin", "h1_margin"):
        o[f"{m}_blend_roster_diff"] = _d(o, f"home_{m}_blend_roster")
    for m in ("est_net_rtg", "margin"):
        for w in ("l20", "l10", "l5"):
            o[f"{m}_{w}_diff"] = _d(o, f"home_{m}_{w}_f")
        o[f"{m}_trend10_diff"] = _d(o, f"home_{m}_trend10")
    for m in ("efg_pct", "tov_pct", "orb_pct", "opp_efg_pct", "opp_tov_pct"):
        o[f"{m}_season_diff"] = _d(o, f"home_{m}_season_f")
    for c in ("ret_min_pct", "starter_continuity", "rotation_continuity", "ret_starter_min_pct", "roster_prior_pm48"):
        o[f"{c}_diff"] = _d(o, f"home_{c}").fillna(0.0)
    o["cont_known"] = (o["home_ret_min_pct"].notna() & o["away_ret_min_pct"].notna()).astype(float)
    o["min_gp"] = o[["home_gp", "away_gp"]].min(axis=1)
    o["early_season"] = (o["min_gp"] < 10).astype(float)
    # 傷病
    known = (o["home_inj_known"] == 1) & (o["away_inj_known"] == 1)
    o["inj_both_known"] = known.astype(float)
    for c in ("inj_min_lost_recent", "inj_min_lost_role", "inj_exp_starters_avail", "inj_rotation_avail_pct",
              "inj_top3_absent_w", "inj_n_out"):
        o[f"{c}_diff"] = _d(o, f"home_{c}").where(known, 0.0).fillna(0.0)
        o[f"{c}_sum"] = (o[f"home_{c}"] + o[f"away_{c}"]).where(known, np.nan)
    for c in ("inj_min_lost_recent_sum", "inj_min_lost_role_sum", "inj_rotation_avail_pct_sum", "inj_n_out_sum"):
        o[c] = o[c].fillna(fill(c, o[c]))
    # 本季 v2 賽程
    v2 = lambda c: c + "_v2" if c + "_v2" in o.columns else c      # 與 Phase C 同名欄位在組裝時加了 _v2
    o["b2b_v2_diff"] = (o[v2("home_b2b")].fillna(0) - o[v2("away_b2b")].fillna(0))
    o["b2b_sum"] = o[v2("home_b2b")].fillna(0) + o[v2("away_b2b")].fillna(0)
    o["rest_sum"] = o[v2("home_rest_days")].fillna(2) + o[v2("away_rest_days")].fillna(2)
    o["games_7d_sum"] = o["home_games_7d"] + o["away_games_7d"]
    o["h2h_season_margin_f"] = o["h2h_season_margin"].fillna(0.0)
    # ---- sum（總分類）---- #
    for mode in ("current", "blend"):
        o[f"exp_pts_{mode}_sum"] = (o[f"home_pts_{mode}"] + o[f"away_opp_pts_{mode}"]
                                    + o[f"away_pts_{mode}"] + o[f"home_opp_pts_{mode}"]) / 2
        o[f"exp_h1_{mode}_sum"] = (o[f"home_h1_pts_{mode}"] + o[f"away_h1_opp_pts_{mode}"]
                                   + o[f"away_h1_pts_{mode}"] + o[f"home_h1_opp_pts_{mode}"]) / 2
        pace = (o[f"home_est_pace_{mode}"] + o[f"away_est_pace_{mode}"]) / 2
        eff = (o[f"home_est_off_rtg_{mode}"] + o[f"away_est_def_rtg_{mode}"]
               + o[f"away_est_off_rtg_{mode}"] + o[f"home_est_def_rtg_{mode}"]) / 2
        o[f"exp_pace_{mode}"] = pace
        o[f"exp_eff_{mode}_sum"] = eff
        o[f"exp_total_poss_{mode}"] = pace * eff / 100.0
    o["pace_l10_sum"] = o["home_est_pace_l10_f"] + o["away_est_pace_l10_f"]
    o["pts_trend10_sum"] = ((o["home_pts_l10"] - o["home_pts_season"]).fillna(0)
                            + (o["away_pts_l10"] - o["away_pts_season"]).fillna(0)
                            + (o["home_opp_pts_l10"] - o["home_opp_pts_season"]).fillna(0)
                            + (o["away_opp_pts_l10"] - o["away_opp_pts_season"]).fillna(0))
    for c in ("total_est", "h1_total_est"):          # Phase C 估計在各隊前 3 場為 NaN → 訓練集平均
        o[c + "_f"] = o[c].fillna(fill(c, o[c]))
    o["league_pts_prev_f"] = o["league_pts_prev"].fillna(fill("league_pts_prev", o["league_pts_prev"]))
    for c in ("ret_min_pct", "starter_continuity"):
        o[f"{c}_sum"] = (o[f"home_{c}"] + o[f"away_{c}"]).fillna(fill(c, o[f"home_{c}"]) * 2)
    return o, fills


WIN_GROUPS: dict[str, list[str]] = {}
WIN_GROUPS["A"] = ["elo_diff"]
WIN_GROUPS["B"] = list(PHASE_C_WIN_FEATURES)
WIN_GROUPS["C"] = WIN_GROUPS["B"] + [
    "est_net_rtg_current_diff", "margin_current_diff", "est_net_rtg_l10_diff", "est_net_rtg_l5_diff",
    "est_net_rtg_trend10_diff", "efg_pct_season_diff", "tov_pct_season_diff", "orb_pct_season_diff",
    "opp_efg_pct_season_diff", "h1_margin_current_diff", "h2h_season_margin_f"]
WIN_GROUPS["D"] = [c.replace("_current_diff", "_blend_diff") for c in WIN_GROUPS["C"]] + [
    "est_net_rtg_blend_roster_diff", "ret_min_pct_diff", "starter_continuity_diff", "roster_prior_pm48_diff"]
WIN_GROUPS["E"] = WIN_GROUPS["D"] + [
    "inj_min_lost_recent_diff", "inj_min_lost_role_diff", "inj_exp_starters_avail_diff",
    "inj_rotation_avail_pct_diff", "inj_top3_absent_w_diff"]
# 精簡版：不用 Phase C 跨季滾動的 form/margin10，只保留 Elo + 本季/先驗混合 + 名單 + 傷病 + 賽程
WIN_GROUPS["E_lean"] = ["elo_diff", "b2b_v2_diff", "rest_diff", "est_net_rtg_blend_roster_diff",
                        "margin_blend_diff", "est_net_rtg_l10_diff", "est_net_rtg_trend10_diff",
                        "inj_min_lost_recent_diff", "inj_exp_starters_avail_diff", "inj_rotation_avail_pct_diff"]

MARGIN_GROUPS = {k: list(v) for k, v in WIN_GROUPS.items()}
H1_MARGIN_GROUPS = {k: list(v) for k, v in WIN_GROUPS.items()}
TOTAL_GROUPS: dict[str, list[str]] = {
    "A": ["total_est_f"],
    "B": ["total_est_f", "h1_total_est_f", "rest_sum", "b2b_sum", "is_playoffs"],
}
TOTAL_GROUPS["C"] = TOTAL_GROUPS["B"] + ["exp_pts_current_sum", "exp_pace_current", "exp_total_poss_current",
                                         "pace_l10_sum", "pts_trend10_sum", "league_pts_prev_f", "games_7d_sum"]
TOTAL_GROUPS["D"] = [c.replace("_current", "_blend") for c in TOTAL_GROUPS["C"]] + ["ret_min_pct_sum"]
TOTAL_GROUPS["E"] = TOTAL_GROUPS["D"] + ["inj_min_lost_recent_sum", "inj_min_lost_role_sum",
                                         "inj_rotation_avail_pct_sum"]
TOTAL_GROUPS["E_lean"] = ["exp_pts_blend_sum", "exp_total_poss_blend", "pace_l10_sum", "league_pts_prev_f",
                          "b2b_sum", "is_playoffs", "inj_min_lost_recent_sum", "inj_rotation_avail_pct_sum"]
H1_TOTAL_GROUPS = {k: [c.replace("exp_pts_", "exp_h1_") for c in v] for k, v in TOTAL_GROUPS.items()}
H1_TOTAL_GROUPS["A"] = ["h1_total_est_f"]

# 傷病只用「人數」（不加權）的對照組：核心球員 Out 與邊緣球員 Out 都只算 1
for _groups in (WIN_GROUPS, MARGIN_GROUPS, H1_MARGIN_GROUPS):
    _groups["E_count"] = _groups["D"] + ["inj_n_out_diff"]
TOTAL_GROUPS["E_count"] = TOTAL_GROUPS["D"] + ["inj_n_out_sum"]
H1_TOTAL_GROUPS["E_count"] = H1_TOTAL_GROUPS["D"] + ["inj_n_out_sum"]

TARGETS = {"win": ("y_home_win", WIN_GROUPS), "margin": ("y_margin", MARGIN_GROUPS),
           "total": ("y_total", TOTAL_GROUPS), "h1_margin": ("y_h1_margin", H1_MARGIN_GROUPS),
           "h1_total": ("y_h1_total", H1_TOTAL_GROUPS)}


# ------------------------------------------------------------------ #
# 模型                                                                 #
# ------------------------------------------------------------------ #

def fit_logistic(X: pd.DataFrame, y, C: float):
    m = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=2000))
    return m.fit(X.fillna(0.0), y)


def fit_ridge(X: pd.DataFrame, y, alpha: float):
    m = make_pipeline(StandardScaler(), Ridge(alpha=alpha))
    return m.fit(X.fillna(0.0), y)


def fit_xgb(X: pd.DataFrame, y, *, classifier: bool, **params):
    import xgboost as xgb
    base = dict(n_estimators=300, learning_rate=0.03, max_depth=3, subsample=0.8, colsample_bytree=0.8,
                min_child_weight=20, reg_lambda=5.0, tree_method="hist", n_jobs=4, random_state=42)
    base.update(params)
    if classifier:
        return xgb.XGBClassifier(objective="binary:logistic", eval_metric="logloss", **base).fit(X, y)
    return xgb.XGBRegressor(objective="reg:squarederror", **base).fit(X, y)


def predict(model, X: pd.DataFrame, *, classifier: bool) -> np.ndarray:
    is_xgb = type(model).__module__.startswith("xgboost")
    Xf = X if is_xgb else X.fillna(0.0)
    return model.predict_proba(Xf)[:, 1] if classifier else model.predict(Xf)


# ------------------------------------------------------------------ #
# 指標                                                                 #
# ------------------------------------------------------------------ #

def win_metrics(p, y) -> dict[str, float]:
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, float)
    ll = -(y * np.log(p) + (1 - y) * np.log(1 - p))
    out = {"n": int(len(y)), "log_loss": float(ll.mean()), "brier": float(((p - y) ** 2).mean()),
           "accuracy": float(((p > 0.5) == (y == 1)).mean())}
    out.update(calibration(p, y))
    return out


def calibration(p, y, bins: int = 10) -> dict[str, Any]:
    p, y = np.asarray(p, float), np.asarray(y, float)
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    ece, table = 0.0, []
    for b in range(bins):
        m = idx == b
        if m.sum() == 0:
            continue
        gap = abs(p[m].mean() - y[m].mean())
        ece += m.mean() * gap
        table.append({"bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}", "n": int(m.sum()),
                      "mean_p": float(p[m].mean()), "obs": float(y[m].mean())})
    logit = np.log(p / (1 - p)).reshape(-1, 1)
    lr = LogisticRegression(C=1e6, max_iter=1000).fit(logit, y)
    return {"ece": float(ece), "cal_slope": float(lr.coef_[0][0]), "cal_intercept": float(lr.intercept_[0]),
            "reliability": table}


def reg_metrics(pred, y, *, directional: bool) -> dict[str, float]:
    pred, y = np.asarray(pred, float), np.asarray(y, float)
    out = {"n": int(len(y)), "mae": float(np.abs(pred - y).mean()), "rmse": float(np.sqrt(((pred - y) ** 2).mean()))}
    if directional:
        m = y != 0
        out["dir_acc"] = float((np.sign(pred[m]) == np.sign(y[m])).mean())
    return out


def per_game_loss(kind: str, pred, y) -> np.ndarray:
    pred, y = np.asarray(pred, float), np.asarray(y, float)
    if kind == "log_loss":
        p = np.clip(pred, 1e-6, 1 - 1e-6)
        return -(y * np.log(p) + (1 - y) * np.log(1 - p))
    if kind == "brier":
        return (pred - y) ** 2
    if kind == "mae":
        return np.abs(pred - y)
    raise ValueError(kind)


def paired_bootstrap(loss_a: np.ndarray, loss_b: np.ndarray, blocks: np.ndarray, *, reps: int = 2000,
                     seed: int = 7) -> dict[str, float]:
    """mean(loss_a − loss_b) 與 95% CI；以比賽日（ET）為區塊重抽，保留同日比賽的相關性。"""
    diff = np.asarray(loss_a) - np.asarray(loss_b)
    uniq, inv = np.unique(blocks, return_inverse=True)
    sums = np.bincount(inv, weights=diff)
    cnts = np.bincount(inv)
    rng = np.random.default_rng(seed)
    stats = np.empty(reps)
    for r in range(reps):
        pick = rng.integers(0, len(uniq), len(uniq))
        stats[r] = sums[pick].sum() / cnts[pick].sum()
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return {"mean_diff": float(diff.mean()), "ci_lo": float(lo), "ci_hi": float(hi),
            "p_better": float((stats < 0).mean())}


def et_day(ts: pd.Series) -> np.ndarray:
    return pd.to_datetime(ts, utc=True).dt.tz_convert(ET).dt.date.astype(str).values


def season_game_bucket(min_gp: pd.Series) -> pd.Series:
    g = min_gp + 1        # 兩隊中「較少者」的本季第幾場
    return pd.cut(g, bins=[0, 5, 10, 20, 10_000], labels=["1-5", "6-10", "11-20", "21+"])
