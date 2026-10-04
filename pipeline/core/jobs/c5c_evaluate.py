"""
Phase C.5C：Temporal modeling & walk-forward re-evaluation
------------------------------------------------------------
    python -m core.jobs.c5c_evaluate [--refresh-inputs] [--no-xgb]

流程（評測賽季 2024-25、2025-26 只在最後跑一次；所有選擇都在驗證賽季 2023-24 上完成）：

  1. 由本機快照（c5c_inputs）建 pregame-v2 特徵：決策時點 early / T-60 / T-15，另建一份「固定常數傷病機率」對照。
  2. 先驗策略評估：各種「本季 vs 上季」混合方式預測下一場實際值的誤差（依本季場次分桶）。
  3. 傷病校準：walk-forward P(absent | status) 與固定常數的比較。
  4. 驗證（train ≤ 2022-23 → 2023-24）：每個 target × 特徵組選 C / alpha / 訓練起始賽季；XGBoost 只在驗證勝出時才進評測。
  5. 評測（walk-forward：2024-25 用 ≤2023-24 訓練；2025-26 用 ≤2024-25 訓練），設定凍結自第 4 步。
  6. 分桶（季初）、決策時點、校準、bootstrap 信賴區間。

結果寫入 pipeline/artifacts/c5c_results.json（gitignore），報告見 docs/phase-c5c-report.md。
不寫資料庫、不產生 production 預測（留給 C.5D）。
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

from ..injury_asof import P_ABSENT
from ..injury_history import NOT_LISTED
from ..models import temporal_features as tf
from ..models import temporal_model as tm
from ..models.ml_features import FEATURE_COLUMNS as PHASE_C_REG_FEATURES
from ..models.ml_features import WIN_FEATURES as PHASE_C_WIN_FEATURES
from . import c5c_inputs

log = logging.getLogger(__name__)

TIMINGS = ["early", "T-60", "T-15"]
DEFAULT_TIMING = "T-60"
VAL_SEASON = "2023-24"
EVAL_SEASONS = ["2024-25", "2025-26"]
TRAIN_STARTS = ["2021-22", "2022-23"]
C_GRID = [0.003, 0.01, 0.03, 0.1, 0.3, 1.0]
ALPHA_GRID = [1.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0]
XGB_GRID = [dict(max_depth=2, n_estimators=300, learning_rate=0.03),
            dict(max_depth=3, n_estimators=300, learning_rate=0.03),
            dict(max_depth=3, n_estimators=600, learning_rate=0.015, min_child_weight=40)]
GROUPS = ["A", "B", "C", "D", "E", "E_count", "E_lean"]
FIXED_P = {**P_ABSENT, "Available": 0.0, NOT_LISTED: 0.0}
OUT_JSON = c5c_inputs.CACHE.parent / "c5c_results.json"


# ------------------------------------------------------------------ #
# 資料組裝                                                              #
# ------------------------------------------------------------------ #

def assemble(df: pd.DataFrame, inp: c5c_inputs.C5CInputs) -> pd.DataFrame:
    """v2 特徵 + Elo + Phase C 特徵 + 每隊該場實際值（y_{side}_{m}，只供收縮參數擬合）。限 Phase C 的賽事集合。"""
    games = {g.game_id: g for g in inp.games}
    rename = {v: k for k, v in tf.EST_RENAME.items()}
    real: dict[str, list] = {f"y_{s}_{m}": [] for s in tm.SIDES for m in tm.BLEND_METRICS}
    for gid in df["game_id"]:
        g = games[gid]
        for side, tid, opp, pts, opts, h1, h1o in (
                ("home", g.home_team_id, g.away_team_id, g.home_pts, g.away_pts, g.home_h1, g.away_h1),
                ("away", g.away_team_id, g.home_team_id, g.away_pts, g.home_pts, g.away_h1, g.home_h1)):
            d = inp.derived.get((gid, tid)) or {}
            for m in tm.BLEND_METRICS:
                if m in rename:
                    v = d.get(rename[m])
                elif m == "pts":
                    v = pts
                elif m == "opp_pts":
                    v = opts
                elif m == "margin":
                    v = pts - opts
                elif m == "h1_pts":
                    v = h1
                elif m == "h1_opp_pts":
                    v = h1o
                else:
                    v = None if h1 is None or h1o is None else h1 - h1o
                real[f"y_{side}_{m}"].append(np.nan if v is None else float(v))
    out = df.assign(**real)
    pc_cols = sorted(set(PHASE_C_WIN_FEATURES) | set(PHASE_C_REG_FEATURES) | {"total_est", "h1_total_est",
                                                                              "is_playoffs", "season_stage"})
    pc = inp.phase_c[["game_id"] + [c for c in pc_cols if c in inp.phase_c.columns and c != "game_id"]]
    pc = pc.drop(columns=[c for c in ("elo_home", "elo_away", "elo_diff") if c in pc.columns])
    # v2 與 Phase C 同名欄位（rest_days / b2b / season_game_no）定義不同 → v2 加 _v2 後綴
    out = out.rename(columns={c: c + "_v2" for c in (set(out.columns) & set(pc.columns)) - {"game_id", "season"}})
    out = out.merge(pc, on="game_id", how="inner", suffixes=("", "_pc"))
    out = out.merge(inp.elo, on="game_id", how="left")
    out["elo_diff"] = out["elo_home"] - out["elo_away"]
    out = out.merge(inp.elo_pred, on="game_id", how="left")
    out["elo_p_calc"] = 1.0 / (1.0 + 10 ** (-(out["elo_diff"] + 100.0) / 400.0))
    out["et_day"] = tm.et_day(out["game_time_utc"])
    return out.dropna(subset=["elo_diff"]).sort_values(["game_time_utc", "game_id"]).reset_index(drop=True)


def build_all(inp, timings=TIMINGS) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    frames, calibs = {}, {}
    for t in timings:
        t0 = time.time()
        # trade_aware=False：保持 C.5C 評估時的 pregame-v2 定義（C.5D 的 v2.1 交易感知名單對應不回頭改寫已凍結的評估）
        df, cal = tf.build_temporal_features(inp.games, inp.derived, inp.players, inp.injury_index, timing=t,
                                             return_calibrator=True, trade_aware=False)
        tf.assert_no_leakage(df)
        frames[t] = assemble(df, inp)
        calibs[t] = cal
        log.info("特徵 %s：%d 場 × %d 欄（%.1fs）", t, len(frames[t]), len(tf.feature_columns(df)), time.time() - t0)
    df, cal = tf.build_temporal_features(inp.games, inp.derived, inp.players, inp.injury_index, timing=DEFAULT_TIMING,
                                         return_calibrator=True, fixed_p_absent=FIXED_P, trade_aware=False)
    tf.assert_no_leakage(df)
    frames["fixedP"] = assemble(df, inp)
    calibs["fixedP"] = cal
    return frames, calibs


# ------------------------------------------------------------------ #
# fold                                                                 #
# ------------------------------------------------------------------ #

def prepare_fold(full: pd.DataFrame, test_season: str):
    train = full[full["season"] < test_season]
    test = full[full["season"] == test_season]
    params = tm.fit_all_blends(train)
    tr, fills = tm.add_model_columns(tm.apply_blends(train, params))
    te, _ = tm.add_model_columns(tm.apply_blends(test, params), fills)
    return tr, te, params


def _y(target: str) -> str:
    return tm.TARGETS[target][0]


def fit_predict(tr, te, target, feats, *, family, hp, train_start):
    ycol = _y(target)
    trn = tr[(tr["season"] >= train_start) & tr[ycol].notna()]
    clf = target == "win"
    X, Xt = trn[feats], te[feats]
    if family == "linear":
        model = tm.fit_logistic(X, trn[ycol].astype(int), hp) if clf else tm.fit_ridge(X, trn[ycol], hp)
    else:
        model = tm.fit_xgb(X, trn[ycol].astype(int) if clf else trn[ycol], classifier=clf, **hp)
    return tm.predict(model, Xt, classifier=clf), model


def score(target, pred, y) -> dict[str, float]:
    m = ~np.isnan(np.asarray(y, float))
    pred, y = np.asarray(pred)[m], np.asarray(y, float)[m]
    if target == "win":
        return tm.win_metrics(pred, y)
    return tm.reg_metrics(pred, y, directional=target in ("margin", "h1_margin"))


def primary(target: str) -> str:
    return "log_loss" if target == "win" else "mae"


def baseline_predictions(tr, te, target) -> np.ndarray:
    """Phase C 既有 baseline：勝負 = Elo；分差/上半場分差 = elo_diff 線性；總分 = 雙方近 10 場攻守平均。"""
    if target == "win":
        return te["elo_p"].fillna(te["elo_p_calc"]).values
    if target in ("margin", "h1_margin"):
        trn = tr.dropna(subset=[_y(target)])
        return LinearRegression().fit(trn[["elo_diff"]], trn[_y(target)]).predict(te[["elo_diff"]])
    col = "total_est" if target == "total" else "h1_total_est"
    return te[col].fillna(tr[_y(target)].mean()).values


def phase_c_predictions(tr, te, target) -> np.ndarray:
    """既有 Phase C 模型原樣重現：勝負 = 5 特徵邏輯迴歸 C=1；其他 = XGBoost（Phase C 參數、全部舊特徵）。"""
    if target == "win":
        m = tm.fit_logistic(tr[PHASE_C_WIN_FEATURES], tr["y_home_win"].astype(int), 1.0)
        return tm.predict(m, te[PHASE_C_WIN_FEATURES], classifier=True)
    trn = tr.dropna(subset=[_y(target)])
    m = tm.fit_xgb(trn[PHASE_C_REG_FEATURES], trn[_y(target)], classifier=False)
    return m.predict(te[PHASE_C_REG_FEATURES])


# ------------------------------------------------------------------ #
# 驗證：選超參數                                                          #
# ------------------------------------------------------------------ #

def select_on_validation(full: pd.DataFrame, use_xgb: bool) -> dict[str, Any]:
    tr, te, _ = prepare_fold(full, VAL_SEASON)
    out: dict[str, Any] = {}
    for target, (ycol, groups) in tm.TARGETS.items():
        y = te[ycol].values
        res: dict[str, Any] = {"baseline": score(target, baseline_predictions(tr, te, target), y),
                               "phase_c": score(target, phase_c_predictions(tr, te, target), y)}
        for g in GROUPS:
            feats = groups[g]
            grid = C_GRID if target == "win" else ALPHA_GRID
            best = None
            trials = []
            for start in TRAIN_STARTS:
                for hp in grid:
                    pred, _ = fit_predict(tr, te, target, feats, family="linear", hp=hp, train_start=start)
                    s = score(target, pred, y)
                    trials.append({"train_start": start, "hp": hp, primary(target): s[primary(target)]})
                    if best is None or s[primary(target)] < best["metrics"][primary(target)]:
                        best = {"family": "linear", "hp": hp, "train_start": start, "metrics": s}
            res[g] = {**best, "trials": trials}
        if use_xgb:
            for g in ("E", "E_lean"):
                best = None
                for start in TRAIN_STARTS:
                    for hp in XGB_GRID:
                        pred, _ = fit_predict(tr, te, target, groups[g], family="xgb", hp=hp, train_start=start)
                        s = score(target, pred, y)
                        if best is None or s[primary(target)] < best["metrics"][primary(target)]:
                            best = {"family": "xgb", "hp": hp, "train_start": start, "metrics": s}
                res[f"{g}_xgb"] = best
        out[target] = res
        log.info("驗證 %s：%s", target, {k: round(v["metrics"][primary(target)] if "metrics" in v
                                                  else v[primary(target)], 4) for k, v in res.items()})
    return out


def choose_production(val: dict[str, Any]) -> dict[str, str]:
    """每個 target 在驗證賽季 primary metric 最佳的候選（只在新特徵組與 XGB 之間選；baseline/phase_c 為對照）。"""
    choice = {}
    for target, res in val.items():
        cands = {k: v["metrics"][primary(target)] for k, v in res.items() if isinstance(v, dict) and "metrics" in v}
        choice[target] = min(cands, key=cands.get)
    return choice


# ------------------------------------------------------------------ #
# 評測                                                                 #
# ------------------------------------------------------------------ #

def evaluate(full: pd.DataFrame, val: dict[str, Any], *, groups=None, include_refs=True,
             seasons=EVAL_SEASONS) -> pd.DataFrame:
    """回傳長表：season, target, model, game_id, pred, y, min_gp, et_day。設定全部取自 val（凍結）。
    XGBoost 候選只有在驗證賽季被選為該 target 的最佳模型時才進評測。"""
    chosen = choose_production(val)
    rows = []
    for season in seasons:
        tr, te, _ = prepare_fold(full, season)
        for target, (ycol, tgroups) in tm.TARGETS.items():
            preds: dict[str, np.ndarray] = {}
            if include_refs:
                preds["baseline"] = baseline_predictions(tr, te, target)
                preds["phase_c"] = phase_c_predictions(tr, te, target)
            for name, cfg in val[target].items():
                if not isinstance(cfg, dict) or "family" not in cfg:
                    continue
                if groups is not None and name not in groups:
                    continue
                if cfg["family"] == "xgb" and chosen[target] != name:
                    continue
                g = name.replace("_xgb", "")
                pred, _ = fit_predict(tr, te, target, tgroups[g], family=cfg["family"], hp=cfg["hp"],
                                      train_start=cfg["train_start"])
                preds[name] = pred
            for name, pred in preds.items():
                rows.append(pd.DataFrame({"season": season, "target": target, "model": name,
                                          "game_id": te["game_id"].values, "pred": pred, "y": te[ycol].values,
                                          "min_gp": te["min_gp"].values, "et_day": te["et_day"].values}))
    return pd.concat(rows, ignore_index=True)


def summarize(preds: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for (target, model), g in preds.groupby(["target", "model"]):
        d = out.setdefault(target, {}).setdefault(model, {})
        for season, gs in g.groupby("season"):
            d[season] = score(target, gs["pred"].values, gs["y"].values)
        d["combined"] = score(target, g["pred"].values, g["y"].values)
    return out


def compare(preds: pd.DataFrame, target: str, a: str, b: str, metric: str | None = None,
            subset: pd.Series | None = None) -> dict[str, Any]:
    """a − b 的每場損失差（負 = a 較好），每季與合併，含日區塊 bootstrap 95% CI。"""
    metric = metric or primary(target)
    p = preds[preds["target"] == target]
    if subset is not None:
        p = p[subset.reindex(p.index).fillna(False).values]
    pa = p[p["model"] == a].set_index(["season", "game_id"])
    pb = p[p["model"] == b].set_index(["season", "game_id"])
    j = pa.join(pb, lsuffix="_a", rsuffix="_b", how="inner").dropna(subset=["y_a"])
    out = {}
    for season, gs in list(j.groupby(level=0)) + [("combined", j)]:
        la = tm.per_game_loss(metric, gs["pred_a"].values, gs["y_a"].values)
        lb = tm.per_game_loss(metric, gs["pred_b"].values, gs["y_a"].values)
        out[season] = {"n": int(len(gs)), **tm.paired_bootstrap(la, lb, gs["et_day_a"].values)}
    return out


def by_bucket(preds: pd.DataFrame, target: str, models: list[str]) -> dict[str, Any]:
    p = preds[(preds["target"] == target) & preds["model"].isin(models)].copy()
    p["bucket"] = tm.season_game_bucket(p["min_gp"]).astype(str)
    out: dict[str, Any] = {}
    for (bucket, model), g in p.groupby(["bucket", "model"]):
        out.setdefault(bucket, {})[model] = score(target, g["pred"].values, g["y"].values)
    return out


# ------------------------------------------------------------------ #
# 先驗策略 / 傷病校準                                                     #
# ------------------------------------------------------------------ #

PRIOR_METRICS = ["est_net_rtg", "margin", "est_off_rtg", "est_def_rtg", "est_pace", "pts"]


def prior_strategy_eval(full: pd.DataFrame, seasons) -> dict[str, Any]:
    """各種混合策略預測「該隊下一場實際值」的 MSE（依本季已賽場數分桶）。參數只用 < 該季的資料擬合。"""
    out: dict[str, Any] = {}
    for season in seasons:
        train = full[full["season"] < season]
        test = full[full["season"] == season]
        for metric in PRIOR_METRICS:
            lf = tm.long_side_frame(test, metric)
            y = lf["y"].values
            ok = ~np.isnan(y)
            l_fb = float(tm.long_side_frame(train, metric)["y"].mean())
            L = lf["L"].fillna(l_fb).values
            strategies = {
                "league_mean": L,
                "prev_season_only": np.where(lf["P"].isna(), L, lf["P"]),
                "current_raw": np.where(lf["gp"] > 0, lf["x"].fillna(pd.Series(L)), L),
                "fixed_70prev_30cur": tm.fixed_weight_blend(lf, 0.7, l_fb),
            }
            params = {}
            for mode in ("current", "prev_const", "blend") + (("blend_roster",) if metric in tm.ROSTER_PRIOR_METRICS else ()):
                bp = tm.fit_blend(train, metric, mode)
                params[mode] = bp.__dict__
                strategies[{"current": "shrunk_current", "prev_const": "shrunk_blend_const_rho",
                            "blend": "shrunk_blend_rho_continuity", "blend_roster": "shrunk_blend_rho_c_roster"}[mode]] = \
                    tm.predict_blend(lf, bp)
            buckets = tm.season_game_bucket(lf["gp"]).astype(str).values
            res: dict[str, Any] = {"params": params}
            for name, est in strategies.items():
                err = (np.asarray(est, float) - y) ** 2
                res[name] = {"all": float(np.nanmean(err[ok]))}
                for b in ("1-5", "6-10", "11-20", "21+"):
                    m = ok & (buckets == b)
                    res[name][b] = float(np.mean(err[m])) if m.any() else None
            res["n"] = {b: int((ok & (buckets == b)).sum()) for b in ("1-5", "6-10", "11-20", "21+")}
            out.setdefault(season, {})[metric] = res
    return out


def injury_calibration_report(calibs: dict[str, tf.InjuryCalibrator]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for timing, cal in calibs.items():
        if timing == "fixedP":
            continue
        logdf = pd.DataFrame(cal.log, columns=["season", "status", "lead_h", "p", "absent"])
        logdf["p_fixed"] = logdf["status"].map(FIXED_P).fillna(0.0)
        res: dict[str, Any] = {"final_table": cal.table(),
                               "season_start": {s: {k: round(v["p_status"], 4) for k, v in t.items()}
                                                for s, t in cal.season_start_tables.items()}}
        per = {}
        for (season, status), g in logdf.groupby(["season", "status"]):
            per.setdefault(season, {})[status] = {"n": int(len(g)), "observed": float(g["absent"].mean()),
                                                  "walk_forward_p": float(g["p"].mean()),
                                                  "fixed_p": float(g["p_fixed"].mean())}
        res["per_season"] = per
        ev = logdf[logdf["season"].isin(EVAL_SEASONS)]
        res["eval_brier"] = {"walk_forward": float(((ev["p"] - ev["absent"]) ** 2).mean()),
                             "fixed": float(((ev["p_fixed"] - ev["absent"]) ** 2).mean()), "n": int(len(ev))}
        res["lead_buckets"] = {f"{s}|{b}": {"n": int(len(g)), "observed": float(g["absent"].mean())}
                               for (s, b), g in logdf.assign(b=np.where(logdf["lead_h"] < tf.LEAD_BUCKET_HOURS, "lt3h", "ge3h"))
                               .groupby(["status", "b"])}
        res["lead_hist"] = {f"{lo}-{hi}h": int(((logdf["lead_h"] >= lo) & (logdf["lead_h"] < hi)).sum())
                            for lo, hi in ((0, 1), (1, 2), (2, 3), (3, 6), (6, 12), (12, 24), (24, 99))}
        out[timing] = res
    return out


def window_study(full: pd.DataFrame, season: str) -> dict[str, Any]:
    """時間尺度比較：單一特徵線性模型的樣本外 MAE（分差用主−客差、總分用主+客和）。
    只取兩隊本季都已賽 ≥ 20 場且有上季資料的比賽，讓各窗口在同一批比賽上比較。"""
    tr, te, _ = prepare_fold(full, season)
    keep = lambda d: d[(d["min_gp"] >= 20) & d["home_prev_n"].gt(0) & d["away_prev_n"].gt(0)]
    tr, te = keep(tr), keep(te)
    out: dict[str, Any] = {"n_train": int(len(tr)), "n_test": int(len(te))}
    for target, ycol, op in (("margin", "y_margin", "diff"), ("total", "y_total", "sum")):
        base = float(np.abs(te[ycol] - tr[ycol].mean()).mean())
        res: dict[str, Any] = {"_intercept_only": base}
        for m in tf.SERIES:
            for w in ("season", "l20", "l10", "l5", "prev", "current", "blend"):
                ch, ca = f"home_{m}_{w}", f"away_{m}_{w}"
                if ch not in tr.columns:
                    continue
                f = (lambda d: d[ch] - d[ca]) if op == "diff" else (lambda d: d[ch] + d[ca])
                xtr, xte = f(tr), f(te)
                ok_tr, ok_te = xtr.notna(), xte.notna()
                if ok_tr.sum() < 200 or ok_te.sum() < 200:
                    continue
                lr = LinearRegression().fit(xtr[ok_tr].values.reshape(-1, 1), tr.loc[ok_tr, ycol])
                pred = lr.predict(xte[ok_te].values.reshape(-1, 1))
                res[f"{m}|{w}"] = float(np.abs(pred - te.loc[ok_te, ycol]).mean())
        if target == "margin":
            x = te["h2h_season_margin"]
            ok = x.notna()
            lr = LinearRegression().fit(tr.loc[tr["h2h_season_margin"].notna(), ["h2h_season_margin"]],
                                        tr.loc[tr["h2h_season_margin"].notna(), ycol])
            res["h2h_season_margin|season"] = float(np.abs(lr.predict(te.loc[ok, ["h2h_season_margin"]])
                                                           - te.loc[ok, ycol]).mean())
            res["_h2h_n"] = int(ok.sum())
        out[target] = res
    return out


def continuity_stats(full: pd.DataFrame) -> dict[str, Any]:
    """各季名單延續性（取每隊本季第 10 場前的值）的分布，以及極端例子。"""
    out: dict[str, Any] = {}
    team_rows = []
    for side in tm.SIDES:
        d = full[full[f"{side}_gp"] == 10][["season", f"{side}_team_id", f"{side}_ret_min_pct",
                                              f"{side}_starter_continuity", f"{side}_rotation_continuity",
                                              f"{side}_ret_starter_min_pct", f"{side}_prev_min_returning_pct",
                                              f"{side}_est_net_rtg_prev", f"{side}_est_net_rtg_season"]]
        d.columns = ["season", "team_id", "ret_min_pct", "starter_continuity", "rotation_continuity",
                     "ret_starter_min_pct", "prev_min_returning_pct", "net_prev", "net_first10"]
        team_rows.append(d)
    t = pd.concat(team_rows).drop_duplicates(["season", "team_id"]).dropna(subset=["ret_min_pct"])
    for season, g in t.groupby("season"):
        out[season] = {c: {"mean": float(g[c].mean()), "min": float(g[c].min()), "max": float(g[c].max())}
                       for c in ("ret_min_pct", "starter_continuity", "rotation_continuity", "ret_starter_min_pct",
                                 "prev_min_returning_pct")}
    t["net_change"] = t["net_first10"] - t["net_prev"]
    t["low_cont"] = t["ret_min_pct"] < t["ret_min_pct"].median()
    out["abs_net_change_by_continuity_half"] = {
        "low_continuity": float(t.loc[t["low_cont"], "net_change"].abs().mean()),
        "high_continuity": float(t.loc[~t["low_cont"], "net_change"].abs().mean()),
        "n_team_seasons": int(len(t))}
    out["corr_ret_min_pct_vs_abs_net_change"] = float(t["ret_min_pct"].corr(t["net_change"].abs()))
    return out


def win_coefficients(full: pd.DataFrame, val: dict[str, Any]) -> dict[str, float]:
    """最後一個評測 fold（≤2024-25 訓練）的勝負 E 模型標準化係數。"""
    tr, te, _ = prepare_fold(full, EVAL_SEASONS[-1])
    cfg = val["win"]["E"]
    _, model = fit_predict(tr, te, "win", tm.WIN_GROUPS["E"], family="linear", hp=cfg["hp"],
                           train_start=cfg["train_start"])
    coefs = model.steps[-1][1].coef_[0]
    return {f: float(c) for f, c in sorted(zip(tm.WIN_GROUPS["E"], coefs), key=lambda x: -abs(x[1]))}


# ------------------------------------------------------------------ #
# main                                                                 #
# ------------------------------------------------------------------ #

def run(refresh_inputs: bool = False, use_xgb: bool = True) -> dict[str, Any]:
    inp = c5c_inputs.load(refresh=refresh_inputs)
    frames, calibs = build_all(inp)
    full = frames[DEFAULT_TIMING]
    results: dict[str, Any] = {"timings": TIMINGS, "default_timing": DEFAULT_TIMING, "val_season": VAL_SEASON,
                               "eval_seasons": EVAL_SEASONS,
                               "n_rows": {t: int(len(f)) for t, f in frames.items()},
                               "eval_n": {s: int((full["season"] == s).sum()) for s in EVAL_SEASONS}}
    log.info("先驗策略評估 …")
    results["prior_strategy"] = prior_strategy_eval(full, [VAL_SEASON] + EVAL_SEASONS)
    results["injury_calibration"] = injury_calibration_report(calibs)
    log.info("驗證賽季選擇 …")
    val = select_on_validation(full, use_xgb)
    results["validation"] = val
    results["production_choice_by_validation"] = choose_production(val)
    log.info("評測（凍結設定）…")
    preds = evaluate(full, val)
    preds.to_csv(OUT_JSON.with_name("c5c_eval_predictions.csv.gz"), index=False)
    results["eval"] = summarize(preds)
    results["window_study"] = {s: window_study(full, s) for s in [VAL_SEASON] + EVAL_SEASONS}
    results["continuity_stats"] = continuity_stats(full)
    results["win_coefficients"] = win_coefficients(full, val)
    cmp_pairs = [("E", "phase_c"), ("E", "baseline"), ("C", "B"), ("D", "C"), ("E", "D"), ("E", "E_count"),
                 ("E_lean", "phase_c"), ("E_lean", "E"), ("B", "phase_c"), ("A", "baseline")]
    results["compare"] = {}
    for target in tm.TARGETS:
        metrics = ["log_loss", "brier"] if target == "win" else ["mae"]
        for a, b in cmp_pairs:
            if not ((preds["target"] == target) & (preds["model"] == a)).any():
                continue
            for metric in metrics:
                results["compare"][f"{target}|{a}-{b}|{metric}"] = compare(preds, target, a, b, metric)
    prod = results["production_choice_by_validation"]
    for target in tm.TARGETS:
        for a, b in ((prod[target], "phase_c"), (prod[target], "baseline")):
            for metric in (["log_loss", "brier"] if target == "win" else ["mae"]):
                results["compare"][f"{target}|PROD:{a}-{b}|{metric}"] = compare(preds, target, a, b, metric)
    # 季初分桶
    results["early_season"] = {t: by_bucket(preds, t, ["baseline", "phase_c", "B", "C", "D", "E", "E_lean"])
                               for t in tm.TARGETS}
    early_mask = preds["min_gp"] < 10
    results["early_compare"] = {}
    for target in tm.TARGETS:
        for a, b in (("D", "C"), ("E", "C"), ("D", "phase_c"), ("E", "phase_c")):
            results["early_compare"][f"{target}|{a}-{b}|min_gp<10"] = compare(preds, target, a, b, subset=early_mask)
    # 決策時點
    log.info("決策時點比較 …")
    dt: dict[str, Any] = {}
    dt_preds = []
    for timing in TIMINGS + ["fixedP"]:
        p = evaluate(frames[timing], val, groups={"D", "E", "E_lean", "E_count"}, include_refs=False)
        p["timing"] = timing
        dt_preds.append(p)
        dt[timing] = summarize(p)
    dtp = pd.concat(dt_preds, ignore_index=True)
    dtp.to_csv(OUT_JSON.with_name("c5c_timing_predictions.csv.gz"), index=False)
    results["decision_time"] = dt
    results["decision_time_compare"] = {}
    for target in tm.TARGETS:
        for g in ("E", "E_lean"):
            for a, b in (("T-15", "early"), ("T-60", "early"), ("T-15", "T-60"), ("T-60", "fixedP")):
                pa = dtp[(dtp["timing"] == a)].assign(model=lambda d: d["model"] + "@" + a)
                pb = dtp[(dtp["timing"] == b)].assign(model=lambda d: d["model"] + "@" + b)
                both = pd.concat([pa, pb], ignore_index=True)
                for metric in (["log_loss", "brier"] if target == "win" else ["mae"]):
                    results["decision_time_compare"][f"{target}|{g}@{a}-{g}@{b}|{metric}"] = compare(
                        both, target, f"{g}@{a}", f"{g}@{b}", metric)
    # 校準（勝負）
    results["calibration"] = {m: results["eval"]["win"][m]["combined"] for m in results["eval"]["win"]}
    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str))
    log.info("結果 → %s", OUT_JSON)
    return results


def main() -> None:
    from ..logging_conf import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser(description="Phase C.5C walk-forward re-evaluation")
    ap.add_argument("--refresh-inputs", action="store_true", help="重新從資料庫載入輸入快照")
    ap.add_argument("--no-xgb", action="store_true")
    args = ap.parse_args()
    run(args.refresh_inputs, use_xgb=not args.no_xgb)


if __name__ == "__main__":
    main()
