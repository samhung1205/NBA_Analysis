"""
Phase C.5E：預測分佈的選擇與 walk-forward 校準評估
------------------------------------------------------------
    python -m core.jobs.c5e_evaluate [--refresh-inputs]

流程（預先固定，評測賽季只跑一次）：
  1. OOS 點預測：production 相同流程的 walk-forward（測試賽季 S 只用 S 之前訓練），2022-23 ~ 2025-26，early / final。
  2. 分佈參數一律「每週重擬合」（與 production 每週重訓相同）：某週比賽的分佈只用該週 ET 週一 00:00 之前
     已結束比賽的 OOS 殘差（含同季較早的週）。
  3. 驗證（2023-24；擬合集 = 2022-23 OOS + 2023-24 較早週）上依預先規則選擇（見 select()）：
       a. 尺度模型的 λ：驗證 RPS 最小
       b. 情境尺度 vs 全域：只有在驗證 RPS 的差（比賽日區塊 bootstrap 95% CI）完全 < 0 時才用情境尺度
       c. empirical vs gaussian：RPS 較低者；差異 CI 含 0 → gaussian（較簡單、有平滑尾巴）
       d. profile：early / final 分開擬合 vs 合併殘差；分開沒有顯著較好 → 合併
  4. 評測（2024-25、2025-26）：凍結的選擇 + 所有候選（只作報告）。
輸出：pipeline/artifacts/c5e_results.json、c5e_oos_predictions.csv.gz、c5e_eval_scores.csv.gz。
不寫資料庫。
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import warnings
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..models import distributions as dist
from ..models import temporal_model as tm
from ..production import oos
from ..production.features import HistoryInputs
from ..timeutil import ET
from . import c5c_inputs

log = logging.getLogger(__name__)

VAL_SEASON = "2023-24"
EVAL_SEASONS = ["2024-25", "2025-26"]
TARGETS = list(oos.REG_TARGETS)
PROFILES = ["early", "final"]
KINDS = ["gaussian", "empirical"]
LAMBDAS = [50.0, 200.0, 1000.0]
HALF_OFFSETS = list(range(-10, 10))               # 半分線：floor(pred) + 0.5 + d
INT_OFFSETS = list(range(-8, 9))                  # 整數線：round(pred) + d
OUT = c5c_inputs.CACHE.parent


def history_from_inputs(inp) -> HistoryInputs:
    st = dict(zip(inp.phase_c["game_id"], inp.phase_c["season_stage"]))
    return HistoryInputs(inp.games, {g.game_id: st.get(g.game_id, "playin") for g in inp.games}, inp.derived,
                         inp.players, inp.injury_index)


def week_start(ts: pd.Series) -> pd.Series:
    """ET 週一 00:00（UTC）。"""
    et = pd.to_datetime(ts, utc=True).dt.tz_convert(ET)
    monday = (et - pd.to_timedelta(et.dt.weekday, unit="D")).dt.normalize()
    return monday.dt.tz_convert("UTC")


def cand_name(kind: str, scaled: bool, lam: float | None) -> str:
    return kind + (f"_scaled@{int(lam)}" if scaled else "")


# ------------------------------------------------------------------ #
# walk-forward 評分                                                    #
# ------------------------------------------------------------------ #

def walk_forward_scores(df: pd.DataFrame, target: str, profile: str, season: str, kind: str, scaled: bool,
                        lam: float | None, *, shared: bool = False, fix_mu: float | None = None) -> pd.DataFrame:
    """season 的每一場（該 profile）：用「該週之前」的 OOS 殘差擬合分佈後評分。shared=True：擬合用兩個 profile 的殘差。"""
    pool = df[(df["target"] == target) & (df["profile"].isin(PROFILES if shared else [profile]))]
    test = df[(df["target"] == target) & (df["profile"] == profile) & (df["season"] == season)].copy()
    test["week"] = week_start(test["game_time_utc"])
    out = []
    for wk, g in test.groupby("week", sort=True):
        fit = pool[pool["game_time_utc"] < wk]
        assert (fit["game_time_utc"] < g["game_time_utc"].min()).all()
        d = dist.fit_distribution(target, fit["resid"].values, kind=kind, scaled=scaled,
                                  ctx=oos.context_arrays(fit) if scaled else None, lam=lam or 200.0, fix_mu=fix_mu)
        ctx = oos.context_arrays(g)
        support, pmf = dist.outcome_pmf(d, g["pred"].values, ctx)
        sc = dist.score_outcomes(support, pmf, g["y"].values)
        half = dist.synthetic_lines(g["pred"].values, HALF_OFFSETS)
        p_half, _ = dist.probability_above_matrix(support, pmf, half)
        ints = np.round(g["pred"].values)[:, None] + np.asarray(INT_OFFSETS)[None, :]
        p_int_above, p_int_push = dist.probability_above_matrix(support, pmf, ints)
        p_pos, _ = dist.probability_above_matrix(support, pmf, np.zeros((len(g), 1)))
        r = pd.DataFrame({"game_id": g["game_id"].values, "game_time_utc": pd.to_datetime(g["game_time_utc"].values, utc=True),
                          "y": g["y"].values, "pred": g["pred"].values, "min_gp": g["min_gp"].values,
                          "inj_both_known": g["inj_both_known"].values, "is_playoffs": g["is_playoffs"].values,
                          "rps": sc["rps"], "nll": sc["nll"], "pit": sc["pit"], "sigma": d.scales(ctx, len(g)),
                          "p_margin_pos": p_pos[:, 0],
                          "n_fit": len(fit)})
        r["p_half"] = list(p_half)
        r["o_half"] = list((g["y"].values[:, None] > half).astype(float))
        r["p_int_above"], r["p_int_push"] = list(p_int_above), list(p_int_push)
        r["o_int_above"] = list((g["y"].values[:, None] > ints).astype(float))
        r["o_int_push"] = list((g["y"].values[:, None] == ints).astype(float))
        # 小分差的機率質量（|k| ≤ 3）
        for k in (-3, -2, -1, 0, 1, 2, 3):
            r[f"pk_{k}"] = np.where(support == k, pmf, 0.0).sum(axis=1)
        out.append(r)
    res = pd.concat(out, ignore_index=True)
    res.insert(0, "candidate", cand_name(kind, scaled, lam) + ("|shared" if shared else ""))
    res.insert(0, "season", season)
    res.insert(0, "profile", profile)
    res.insert(0, "target", target)
    res["et_day"] = tm.et_day(res["game_time_utc"])
    return res


def boot(a: pd.DataFrame, b: pd.DataFrame, metric: str = "rps") -> dict[str, float]:
    """a − b（每場，以 profile+game 對齊），比賽日區塊 bootstrap。"""
    j = a.set_index(["profile", "game_id"])[[metric, "et_day"]].join(
        b.set_index(["profile", "game_id"])[[metric]], rsuffix="_b", how="inner")
    return tm.paired_bootstrap(j[metric].values, j[metric + "_b"].values, j["et_day"].values)


def summarize(s: pd.DataFrame) -> dict[str, Any]:
    p_half = np.vstack(s["p_half"].values)
    o_half = np.vstack(s["o_half"].values)
    pa, pp = np.vstack(s["p_int_above"].values), np.vstack(s["p_int_push"].values)
    oa, op = np.vstack(s["o_int_above"].values), np.vstack(s["o_int_push"].values)
    near = (p_half > 0.3) & (p_half < 0.7)
    out = {"n": int(len(s)), "rps": float(s["rps"].mean()), "nll": float(s["nll"].mean()),
           "coverage": dist.coverage(s["pit"].values), "pit": dist.pit_histogram(s["pit"].values),
           "sigma_mean": float(s["sigma"].mean()),
           "half_line": dist.reliability(p_half, o_half, np.round(np.arange(0, 1.0001, 0.05), 2)),
           "half_line_near50": dist.reliability(p_half[near], o_half[near],
                                                np.round(np.arange(0.30, 0.7001, 0.05), 2)),
           "int_line_push": {"pred": float(pp.mean()), "obs": float(op.mean()),
                             "by_offset": {int(d): {"pred": float(pp[:, i].mean()), "obs": float(op[:, i].mean())}
                                           for i, d in enumerate(INT_OFFSETS) if abs(d) <= 4}},
           "int_line_above": {"pred": float(pa.mean()), "obs": float(oa.mean())},
           "small_values": {str(k): {"pred": float(s[f"pk_{k}"].mean()), "obs": float((s["y"] == k).mean())}
                            for k in (-3, -2, -1, 0, 1, 2, 3)}}
    return out


# ------------------------------------------------------------------ #
# 選擇（驗證賽季）                                                        #
# ------------------------------------------------------------------ #

def select(df: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    choice: dict[str, Any] = {}
    all_scores = []
    for target in TARGETS:
        cand_scores: dict[str, pd.DataFrame] = {}
        for kind in KINDS:
            cand_scores[cand_name(kind, False, None)] = pd.concat(
                [walk_forward_scores(df, target, p, VAL_SEASON, kind, False, None) for p in PROFILES])
            for lam in LAMBDAS:
                cand_scores[cand_name(kind, True, lam)] = pd.concat(
                    [walk_forward_scores(df, target, p, VAL_SEASON, kind, True, lam) for p in PROFILES])
        rps = {k: float(v["rps"].mean()) for k, v in cand_scores.items()}
        trace: dict[str, Any] = {"val_rps": rps}
        best_per_kind = {}
        for kind in KINDS:
            lam_best = min(LAMBDAS, key=lambda l: rps[cand_name(kind, True, l)])
            g, c = cand_name(kind, False, None), cand_name(kind, True, lam_best)
            b = boot(cand_scores[c], cand_scores[g])
            use_scaled = b["ci_hi"] < 0
            trace[f"{kind}_scaled_vs_global"] = {"lambda": lam_best, **b, "use_scaled": use_scaled}
            best_per_kind[kind] = (c if use_scaled else g, use_scaled, lam_best if use_scaled else None)
        ge, em = best_per_kind["gaussian"], best_per_kind["empirical"]
        b = boot(cand_scores[em[0]], cand_scores[ge[0]])
        pick = em if b["ci_hi"] < 0 else ge
        trace["empirical_vs_gaussian"] = {**b, "pick": pick[0]}
        kind = pick[0].split("_scaled")[0]
        sep = cand_scores[pick[0]]
        shared = pd.concat([walk_forward_scores(df, target, p, VAL_SEASON, kind, pick[1], pick[2], shared=True)
                            for p in PROFILES])
        bs = {p: boot(sep[sep.profile == p], shared[shared.profile == p]) for p in PROFILES}
        separate = any(v["ci_hi"] < 0 for v in bs.values())
        trace["separate_vs_shared"] = {**bs, "separate": separate}
        choice[target] = {"kind": kind, "scaled": pick[1], "lambda": pick[2], "shared_profiles": not separate,
                          "trace": trace}
        all_scores += list(cand_scores.values()) + [shared]
        log.info("驗證 %s → %s%s（%s）", target, pick[0], "" if separate else "，profile 共用", rps)
    return choice, pd.concat(all_scores, ignore_index=True)


# ------------------------------------------------------------------ #
# 敘述統計：異質變異                                                      #
# ------------------------------------------------------------------ #

def heteroskedasticity(df: pd.DataFrame, seasons: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    d = df[df["season"].isin(seasons)].copy()
    d["gp_bucket"] = tm.season_game_bucket(d["min_gp"]).astype(str)
    for (target, profile), g in d.groupby(["target", "profile"]):
        res: dict[str, Any] = {"all": {"n": int(len(g)), "sd": float(g["resid"].std())}}
        lvl = g["pred"].abs() if "margin" in target else g["pred"]
        g = g.assign(q=pd.qcut(lvl, 5, labels=False, duplicates="drop"))
        res["pred_quintile"] = {int(k): {"n": int(len(x)), "mean_level": float((x["pred"].abs() if "margin" in target else x["pred"]).mean()),
                                         "sd": float(x["resid"].std())} for k, x in g.groupby("q")}
        res["games_played"] = {k: {"n": int(len(x)), "sd": float(x["resid"].std())} for k, x in g.groupby("gp_bucket")}
        res["injury_unknown"] = {str(k): {"n": int(len(x)), "sd": float(x["resid"].std())}
                                 for k, x in g.groupby(g["inj_both_known"] < 1)}
        res["playoffs"] = {str(int(k)): {"n": int(len(x)), "sd": float(x["resid"].std())} for k, x in g.groupby("is_playoffs")}
        out[f"{target}|{profile}"] = res
    return out


def profile_variance(df: pd.DataFrame, seasons: list[str], reps: int = 2000) -> dict[str, Any]:
    """early vs final 殘差變異比（同一批比賽；比賽日區塊 bootstrap）。"""
    out = {}
    for target in TARGETS:
        e = df[(df.target == target) & (df.profile == "early") & df.season.isin(seasons)].set_index("game_id")
        f = df[(df.target == target) & (df.profile == "final") & df.season.isin(seasons)].set_index("game_id")
        j = e[["resid"]].join(f[["resid"]], lsuffix="_e", rsuffix="_f", how="inner")
        j["day"] = tm.et_day(e.loc[j.index, "game_time_utc"])
        days = j["day"].values
        uniq, inv = np.unique(days, return_inverse=True)
        se = np.bincount(inv, j["resid_e"].values ** 2)
        sf = np.bincount(inv, j["resid_f"].values ** 2)
        rng = np.random.default_rng(3)
        ratios = []
        for _ in range(reps):
            pick = rng.integers(0, len(uniq), len(uniq))
            ratios.append(se[pick].sum() / sf[pick].sum())
        lo, hi = np.percentile(ratios, [2.5, 97.5])
        out[target] = {"n": int(len(j)), "sd_early": float(np.sqrt((j.resid_e ** 2).mean())),
                       "sd_final": float(np.sqrt((j.resid_f ** 2).mean())),
                       "mse_ratio_early_over_final": float((j.resid_e ** 2).mean() / (j.resid_f ** 2).mean()),
                       "ci": [float(lo), float(hi)]}
    return out


def diagnostics(df: pd.DataFrame, ev: pd.DataFrame, choice: dict[str, Any]) -> dict[str, Any]:
    """(1) 位置參數敏感度：μ 固定為 0 的同一分佈；(2) 分差分佈的 P(主勝) vs 邏輯迴歸勝率。皆只作報告。"""
    out: dict[str, Any] = {"location_mu_by_season": {}, "mu0": {}, "win_vs_margin": {}}
    for (t, s), g in df[(df.profile == "final") & (df.target != "win")].groupby(["target", "season"]):
        out["location_mu_by_season"][f"{t}|{s}"] = float(g["resid"].mean())
    for target in TARGETS:
        ch = choice[target]
        parts = [walk_forward_scores(df, target, p, s, ch["kind"], ch["scaled"], ch["lambda"],
                                     shared=ch["shared_profiles"], fix_mu=0.0) for s in EVAL_SEASONS for p in PROFILES]
        mu0 = pd.concat(parts, ignore_index=True)
        sel = ev[(ev.target == target) & (ev.candidate == "SELECTED")]
        sm = summarize(mu0)
        out["mu0"][target] = {"rps": sm["rps"], "coverage": sm["coverage"], "half_line_ece": sm["half_line"]["ece"],
                              "near50_ece": sm["half_line_near50"]["ece"],
                              "selected_near50_ece": summarize(sel)["half_line_near50"]["ece"],
                              "rps_mu0_minus_selected": boot(mu0, sel)}
    # 勝率一致性：OOS 邏輯迴歸勝率 vs 分差分佈的 P(margin > 0)（同一批評測比賽）
    sel = ev[(ev.target == "margin") & (ev.candidate == "SELECTED")]
    win = df[df.target == "win"][["profile", "game_id", "pred", "y"]].rename(columns={"pred": "p_win", "y": "y_win"})
    j = sel.merge(win, on=["profile", "game_id"], how="inner")
    if len(j):
        pm, pw, y = j["p_margin_pos"].values, j["p_win"].values, j["y_win"].values
        ll = lambda p: float(-(y * np.log(np.clip(p, 1e-6, 1)) + (1 - y) * np.log(np.clip(1 - p, 1e-6, 1))).mean())
        out["win_vs_margin"] = {"n": int(len(j)), "corr": float(np.corrcoef(pm, pw)[0, 1]),
                                "mean_abs_diff": float(np.abs(pm - pw).mean()),
                                "p90_abs_diff": float(np.quantile(np.abs(pm - pw), 0.9)),
                                "log_loss_logistic": ll(pw), "log_loss_margin_dist": ll(pm),
                                "brier_logistic": float(((pw - y) ** 2).mean()),
                                "brier_margin_dist": float(((pm - y) ** 2).mean())}
    return out


# ------------------------------------------------------------------ #
# 獨贏機率：分差分佈的 P(margin > 0) vs 專用邏輯迴歸勝率（Phase D 定價用；不調模型）        #
# ------------------------------------------------------------------ #

# 非劣性界線在執行比較之前就固定（2026-10-04）：
#   分差導出機率 − 邏輯迴歸 的每場損失差，比賽日區塊 bootstrap 95% CI 上界 < δ → 非劣
#   log loss δ = 0.0025（≈ C.5C 模型相對 Phase C 改善 0.0124 的 1/5）；Brier δ = 0.0010（同比例）
NI_DELTA = {"log_loss": 0.0025, "brier": 0.0010}


def _win_losses(p: np.ndarray, y: np.ndarray) -> dict[str, np.ndarray]:
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    return {"log_loss": -(y * np.log(pc) + (1 - y) * np.log(1 - pc)), "brier": (p - y) ** 2}


def _win_block(j: pd.DataFrame) -> dict[str, Any]:
    y = j["y_win"].values
    out: dict[str, Any] = {"n": int(len(j))}
    for name, col in (("margin_derived", "p_margin_pos"), ("logistic", "p_win")):
        m = tm.win_metrics(j[col].values, y)
        out[name] = {k: m[k] for k in ("log_loss", "brier", "accuracy", "ece", "cal_slope", "cal_intercept")}
        out[name]["reliability"] = m["reliability"]
    la, lb = _win_losses(j["p_margin_pos"].values, y), _win_losses(j["p_win"].values, y)
    out["diff_margin_minus_logistic"] = {}
    for metric, delta in NI_DELTA.items():
        b = tm.paired_bootstrap(la[metric], lb[metric], j["et_day"].values)
        out["diff_margin_minus_logistic"][metric] = {**b, "ni_delta": delta, "non_inferior": b["ci_hi"] < delta}
    out["prob_agreement"] = {"corr": float(np.corrcoef(j["p_margin_pos"], j["p_win"])[0, 1]),
                             "mean_abs_diff": float(np.abs(j["p_margin_pos"] - j["p_win"]).mean()),
                             "p90_abs_diff": float(np.quantile(np.abs(j["p_margin_pos"] - j["p_win"]), 0.9)),
                             "same_favorite": float(((j["p_margin_pos"] > 0.5) == (j["p_win"] > 0.5)).mean())}
    return out


def moneyline_comparison(df: pd.DataFrame, df_win: pd.DataFrame) -> dict[str, Any]:
    """同一批 OOS 比賽（每 profile、每季）：分差分佈（每週重擬合的全域高斯、兩 profile 共用）導出的 P(主勝)
    vs OOS 邏輯迴歸勝率。兩種中心：oos_mean（預先登記）與 zero（評測後修正；評測季結果不作確認性證據）。"""
    win = df_win[["profile", "game_id", "pred", "y"]].rename(columns={"pred": "p_win", "y": "y_win"})
    out: dict[str, Any] = {"ni_delta": NI_DELTA, "note": "zero 中心在 2024-25 / 2025-26 的結果是評測後選擇，非確認性證據"}
    for loc, fix in (("oos_mean", None), ("zero", 0.0)):
        parts = [walk_forward_scores(df, "margin", p, s, "gaussian", False, None, shared=True, fix_mu=fix)
                 for s in [VAL_SEASON] + EVAL_SEASONS for p in PROFILES]
        sc = pd.concat(parts, ignore_index=True)[["profile", "season", "game_id", "et_day", "p_margin_pos"]]
        j = sc.merge(win, on=["profile", "game_id"], how="inner")
        res: dict[str, Any] = {}
        for season, g in j.groupby("season"):
            res[season] = _win_block(g)
            for prof, gp in g.groupby("profile"):
                res[f"{season}|{prof}"] = _win_block(gp)
        ev = j[j.season.isin(EVAL_SEASONS)]
        res["eval_combined"] = _win_block(ev)
        for prof, gp in ev.groupby("profile"):
            res[f"eval_combined|{prof}"] = _win_block(gp)
        res["val_and_eval_combined"] = _win_block(j)          # 2023-24 + 2024-25 + 2025-26
        out[loc] = res
    return out


# ------------------------------------------------------------------ #
# main                                                                 #
# ------------------------------------------------------------------ #

def run(refresh_inputs: bool = False) -> dict[str, Any]:
    t0 = time.time()
    inp = c5c_inputs.load(refresh=refresh_inputs)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        df = oos.walk_forward_oos(history_from_inputs(inp), targets=TARGETS + ["win"])
    df.to_csv(OUT / "c5e_oos_predictions.csv.gz", index=False)
    df_win, df = df[df.target == "win"], df[df.target != "win"].reset_index(drop=True)
    log.info("OOS 預測 %d 列（%.0fs）", len(df), time.time() - t0)
    results: dict[str, Any] = {"val_season": VAL_SEASON, "eval_seasons": EVAL_SEASONS,
                               "audit": df.groupby(["profile", "target", "season"]).agg(
                                   n=("resid", "size"), mean=("resid", "mean"), sd=("resid", "std"),
                                   mae=("resid", lambda r: r.abs().mean())).reset_index().to_dict("records"),
                               "context_share": {p: {"min_gp<10": float((g.min_gp < 10).mean()),
                                                     "injury_unknown": float((g.inj_both_known < 1).mean()),
                                                     "playoffs": float(g.is_playoffs.mean())}
                                                 for p, g in df[df.target == "margin"].groupby("profile")}}
    results["heteroskedasticity_pre_eval"] = heteroskedasticity(df, ["2022-23", VAL_SEASON])
    results["heteroskedasticity_eval"] = heteroskedasticity(df, EVAL_SEASONS)
    results["profile_variance_pre_eval"] = profile_variance(df, ["2022-23", VAL_SEASON])
    results["profile_variance_eval"] = profile_variance(df, EVAL_SEASONS)

    choice, val_scores = select(df)
    results["selection"] = choice
    results["validation"] = {f"{t}|{c}": summarize(g) for (t, c), g in val_scores.groupby(["target", "candidate"])}
    log.info("驗證選擇完成（%.0fs）", time.time() - t0)

    # ---- 評測：凍結的選擇 + 對照候選 ---- #
    evals = []
    for target in TARGETS:
        ch = choice[target]
        lam_by_kind = {k: ch["trace"][f"{k}_scaled_vs_global"]["lambda"] for k in KINDS}
        for season in EVAL_SEASONS:
            for p in PROFILES:
                for kind in KINDS:
                    evals.append(walk_forward_scores(df, target, p, season, kind, False, None))
                    evals.append(walk_forward_scores(df, target, p, season, kind, True, lam_by_kind[kind]))
                sel = walk_forward_scores(df, target, p, season, ch["kind"], ch["scaled"], ch["lambda"],
                                          shared=ch["shared_profiles"])
                sel["candidate"] = "SELECTED"
                evals.append(sel)
    ev = pd.concat(evals, ignore_index=True)
    results["eval"] = {}
    for (target, cand), g in ev.groupby(["target", "candidate"]):
        d = {"combined": summarize(g)}
        for (season, prof), gs in g.groupby(["season", "profile"]):
            d[f"{season}|{prof}"] = {k: v for k, v in summarize(gs).items() if k in ("n", "rps", "nll", "coverage", "sigma_mean")}
        for prof, gs in g.groupby("profile"):
            d[prof] = summarize(gs)
        results["eval"][f"{target}|{cand}"] = d
    results["eval_compare"] = {}
    for target in TARGETS:
        g = ev[ev.target == target]
        lam = {k: choice[target]["trace"][f"{k}_scaled_vs_global"]["lambda"] for k in KINDS}
        pairs = [(cand_name("gaussian", True, lam["gaussian"]), "gaussian"),
                 (cand_name("empirical", True, lam["empirical"]), "empirical"),
                 ("empirical", "gaussian"), ("SELECTED", "gaussian")]
        for a, b in pairs:
            for scope, gg in [("combined", g)] + [(s, g[g.season == s]) for s in EVAL_SEASONS]:
                results["eval_compare"][f"{target}|{a}-{b}|{scope}"] = boot(gg[gg.candidate == a], gg[gg.candidate == b])
    # ---- 只作報告的診斷（不參與選擇；選擇已在驗證賽季凍結）---- #
    results["diagnostics_not_used_for_selection"] = diagnostics(pd.concat([df, df_win]), ev, choice)
    results["moneyline_comparison"] = moneyline_comparison(df, df_win)
    keep = ["target", "profile", "season", "candidate", "game_id", "game_time_utc", "y", "pred", "rps", "nll", "pit", "sigma"]
    ev[keep].to_csv(OUT / "c5e_eval_scores.csv.gz", index=False)
    (OUT / "c5e_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str))
    log.info("完成（%.0fs）→ %s", time.time() - t0, OUT / "c5e_results.json")
    return results


def main() -> None:
    from ..logging_conf import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser(description="Phase C.5E predictive distributions")
    ap.add_argument("--refresh-inputs", action="store_true")
    args = ap.parse_args()
    run(args.refresh_inputs)


if __name__ == "__main__":
    main()
