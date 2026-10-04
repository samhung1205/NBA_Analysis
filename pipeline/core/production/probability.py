"""
盤口線機率介面（C.5E）——Phase D 會用這裡，但這裡**不**計算 edge / EV / 去水 / Kelly
------------------------------------------------------------
    predict_margin_probability(game_prediction, line, art)      P(主隊分差 > line)
    predict_total_probability(game_prediction, line, art)       P(總分 > line)
    predict_h1_margin_probability / predict_h1_total_probability
    line_probability_from_prediction_row(row, target, line)     由 predictions 表的一列（含 features_json）重算

line 可以是任意實數：整數線會回傳 push 機率；半分線 push = 0；四分之一線 push = 0
（拆成兩條半注是 bookmaker adapter 的事，distributions.quarter_line_components 只做數學拆解）。
「主隊讓 5.5 分」對應 P(margin > 5.5)；「客隊受讓 5.5」是 probability_below。

決定性：同一個 artifact 版本、同一個點預測與線 → 逐位元相同的輸出。artifact 版本目錄不可變，
所以用 features_json.artifact_version 重算歷史預測的機率永遠得到當時的結果。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ..models import distributions as dist
from . import artifact as art_mod
from . import spec

QUALITY_NOTE = ("資料品質旗標不改變分佈寬度：C.5E 樣本外評估中情境尺度模型（季初 / 傷病未知 / 預測水準 / 季後賽）"
                "沒有顯著改善；pending_prior_game / stale_results / missing_box_recent 在歷史資料中沒有樣本可驗證。"
                "旗標只作為 UI 警示。")


def distribution_context(min_gp: float, inj_both_known: float, is_playoffs: float) -> dict[str, float]:
    return {"min_gp": float(min_gp), "inj_both_known": float(inj_both_known), "is_playoffs": float(is_playoffs)}


def _ctx(context: dict[str, float] | None, pred: float) -> dict[str, np.ndarray] | None:
    if context is None:
        return None
    return dist.context_features(np.array([pred]), np.array([context["min_gp"]]),
                                 np.array([context["inj_both_known"]]), np.array([context["is_playoffs"]]))


def predict_line_probability(art: art_mod.LoadedArtifact, profile: str, target: str, pred: float, line: float, *,
                             context: dict[str, float] | None = None, flags: list[str] | None = None
                             ) -> dict[str, Any]:
    if target not in spec.DISTRIBUTION_TARGETS:
        raise ValueError(f"target 必須是 {spec.DISTRIBUTION_TARGETS}")
    if not np.isfinite(pred) or not np.isfinite(line):
        raise ValueError("pred / line 必須是有限值")
    dd = art.profile(profile)["distributions"][target]
    d = dist.FittedDistribution.from_state(dd["state"])
    if d.scale_model and context is None:
        raise ValueError(f"{target} 的分佈依情境調整尺度，需要 context")
    lp = dist.line_probabilities(d, float(pred), [float(line)], _ctx(context, float(pred)) if d.scale_model else None)[0]
    return {
        "target": target, "line": lp.line, "point_prediction": float(pred),
        "probability_above": lp.probability_above, "probability_push": lp.probability_push,
        "probability_below": lp.probability_below,
        "distribution": lp.distribution, "distribution_version": dd["distribution_version"],
        "center": lp.center, "scale": lp.scale,
        "artifact_version": art.artifact_version, "model_version": art.model_version, "profile": profile,
        "fit": {"n_fit": d.n_fit, "fit_start_utc": d.fit_start_utc, "fit_end_utc": d.fit_end_utc,
                "oos_seasons": dd.get("oos_seasons"), "shared_profiles": dd.get("shared_profiles"),
                "location_rule": dd["spec"].get("location"), "fit_set_coverage": dd["fit_set_summary"]["coverage"]},
        "data_quality": {"flags": list(flags or []), "affects_distribution": bool(d.scale_model),
                         "note": QUALITY_NOTE},
    }


def _point(game_prediction, target: str) -> float:
    p = game_prediction
    return {"margin": p.pred_margin, "total": p.pred_total, "h1_margin": p.pred_home_h1 - p.pred_away_h1,
            "h1_total": p.pred_home_h1 + p.pred_away_h1}[target]


def _from_game(game_prediction, target: str, line: float, art) -> dict[str, Any]:
    if game_prediction.features_json.get("artifact_version") != art.artifact_version:
        raise ValueError("game_prediction 與 artifact 版本不同")
    return predict_line_probability(art, game_prediction.profile, target, _point(game_prediction, target), line,
                                    context=game_prediction.features_json.get("distribution_context"),
                                    flags=game_prediction.quality.flags)


def predict_margin_probability(game_prediction, line: float, art) -> dict[str, Any]:
    return _from_game(game_prediction, "margin", line, art)


def predict_total_probability(game_prediction, line: float, art) -> dict[str, Any]:
    return _from_game(game_prediction, "total", line, art)


def predict_h1_margin_probability(game_prediction, line: float, art) -> dict[str, Any]:
    return _from_game(game_prediction, "h1_margin", line, art)


def predict_h1_total_probability(game_prediction, line: float, art) -> dict[str, Any]:
    return _from_game(game_prediction, "h1_total", line, art)


def line_probability_from_prediction_row(row: dict[str, Any], target: str, line: float, *,
                                         root: str | Path | None = None,
                                         art: art_mod.LoadedArtifact | None = None) -> dict[str, Any]:
    """predictions 表的一列（含 features_json）→ 用「當時的 artifact 版本」重算機率（版本目錄不可變）。
    art：呼叫端已載入的同一版本 artifact（D.2 定價一次處理多條線時避免重複載入）；版本不同 → ValueError。"""
    fj = row["features_json"] or {}
    if art is None:
        art = art_mod.load_version(fj["artifact_version"], root)
    elif art.artifact_version != fj["artifact_version"]:
        raise ValueError(f"prediction 的 artifact_version {fj['artifact_version']} ≠ 傳入的 {art.artifact_version}")
    pred = {"margin": row["pred_margin"], "total": row["pred_total"],
            "h1_margin": row["pred_home_h1"] - row["pred_away_h1"],
            "h1_total": row["pred_home_h1"] + row["pred_away_h1"]}[target]
    return predict_line_probability(art, fj["profile"], target, float(pred), line,
                                    context=fj.get("distribution_context"),
                                    flags=(fj.get("data_quality") or {}).get("flags"))
