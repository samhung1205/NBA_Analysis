"""
Production 推論（純邏輯，不碰 DB）：套用 artifact → 預測、解釋、資料品質、信心度、features_json
------------------------------------------------------------
解釋（features_json）：
  * 勝負（邏輯迴歸）contributions = 標準化係數 × 標準化特徵值，單位 log-odds；Σ + 截距 = 預測 logit（精確分解）。
    正值 = 有利主隊（前端「特徵拆解」既有的方向）。
  * 分差 / 總分 / 上半場（Ridge）target_contributions：同樣是標準化係數 × 標準化特徵值（單位：分），
    Σ + 截距 = 預測值。這是線性模型的精確分解，**不是 SHAP**。

信心度：confidence = |勝率 − 0.5| × 2 × 資料品質係數（係數為啟發式，見 QUALITY_FACTORS；原始值另存 confidence_raw）。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..injury_asof import team_state_asof
from ..models import temporal_model as tm
from ..timeutil import ensure_utc, et_date
from . import features as pf
from . import spec
from .artifact import LoadedArtifact

MIN_LEAD = timedelta(minutes=5)          # 距開賽不到 5 分鐘不再產生預測（避免跨過開賽）
TOP_K = 8

# 資料品質旗標 → 信心度係數（啟發式：反映「這場的輸入比訓練時典型的少 / 舊」；不改變預測值本身）
QUALITY_FACTORS = {
    "season_opener": 0.6,          # 任一隊本季第一場：本季樣本 0、名單延續性未知
    "low_sample": 0.75,            # 任一隊本季已賽 < 5 場
    "early_season": 0.9,           # 任一隊本季已賽 < 10 場（C.5C：季初改善未被證實）
    "injury_unknown": 0.8,         # 沒有任何已申報報告涵蓋該隊（傷病特徵以中性值處理）
    "injury_feed_stale": 0.9,      # 傷病來源超過 24 小時沒有新報告
    "pending_prior_game": 0.9,     # 該隊前一場已排定但尚未完賽（近況停在更早一場）
    "stale_results": 0.8,          # 該隊有應已結束但結果未同步的比賽（來源部分失敗）
    "missing_box_recent": 0.85,    # 該隊近 10 場有 ≥ 3 場缺 box score / 衍生指標
    "stage_out_of_training": 0.9,  # 附加賽：不在訓練賽事集合
}
MIN_FACTOR = 0.25
# 同一組內的旗標是巢狀的（開季第一場 ⊂ 樣本 < 5 ⊂ 樣本 < 10），只取該組最嚴重者，避免重複懲罰
QUALITY_GROUPS = {"season_opener": "sample", "low_sample": "sample", "early_season": "sample",
                  "injury_unknown": "injury", "injury_feed_stale": "injury",
                  "stale_results": "results", "pending_prior_game": "results"}


def _num(v: Any, nd: int = 4) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, nd)


# ------------------------------------------------------------------ #
# 模型                                                                  #
# ------------------------------------------------------------------ #

def model_inputs(prof: dict[str, Any], frame: pd.DataFrame, target: str) -> pd.DataFrame:
    """推論的模型輸入 = artifact 記錄的特徵清單與順序（不重新選特徵），NaN → 0（與訓練相同）。"""
    feats = prof["models"][target]["features"]
    missing = [f for f in feats if f not in frame.columns]
    if missing:
        raise KeyError(f"{target}：模型輸入表缺少欄位 {missing}")
    return frame[feats].astype(float).fillna(0.0)


def predict_profile(prof: dict[str, Any], frame: pd.DataFrame) -> dict[str, np.ndarray]:
    out = {}
    for target in spec.PRODUCTION_SPEC:
        est = prof["models"][target]["estimator"]
        X = model_inputs(prof, frame, target)
        out[target] = est.predict_proba(X)[:, 1] if target == "win" else est.predict(X)
    return out


def linear_contributions(est, X: pd.DataFrame) -> tuple[float, np.ndarray]:
    """(截距, 每列每特徵貢獻)：pred（logit 或分數）= 截距 + Σ 貢獻。"""
    scaler, lin = est.steps[0][1], est.steps[-1][1]
    z = scaler.transform(X)
    coef = np.ravel(lin.coef_)
    intercept = float(np.ravel(lin.intercept_)[0]) if np.ndim(lin.intercept_) else float(lin.intercept_)
    return intercept, z * coef


def _top(feats: list[str], contrib: np.ndarray, k: int = TOP_K) -> list[dict[str, Any]]:
    order = np.argsort(-np.abs(contrib))[:k]
    return [{"label": spec.FEATURE_LABELS.get(feats[i], feats[i]), "feature": feats[i],
             "value": round(float(contrib[i]), 3)} for i in order]


TARGET_UNITS = {"margin": "分（主 − 客；正 = 有利主隊）", "total": "分（全場總分；正 = 總分較高）",
                "h1_margin": "分（上半場主 − 客）", "h1_total": "分（上半場總分）"}


# ------------------------------------------------------------------ #
# 資料品質                                                              #
# ------------------------------------------------------------------ #

def team_box_gaps(history: pf.HistoryInputs) -> dict[int, int]:
    """各隊最近 10 場已完賽比賽中，缺 box score 衍生指標或球員分鐘的場數。"""
    per_team: dict[int, list[int]] = defaultdict(list)
    for g in sorted(history.games, key=lambda g: (g.game_time_utc, g.game_id)):
        per_team[g.home_team_id].append(g.game_id)
        per_team[g.away_team_id].append(g.game_id)
    out = {}
    for tid, gids in per_team.items():
        n = 0
        for gid in gids[-10:]:
            lines = [r for r in history.players.get(gid, []) if r[0] == tid and float(r[2] or 0) > 0]
            if (gid, tid) not in history.derived or not lines:
                n += 1
        out[tid] = n
    return out


@dataclass
class GameQuality:
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def factor(self) -> float:
        worst: dict[str, float] = {}
        for k in {f.split(":")[0] for f in self.flags}:
            if k in QUALITY_FACTORS:
                g = QUALITY_GROUPS.get(k, k)
                worst[g] = min(worst.get(g, 1.0), QUALITY_FACTORS[k])
        f = 1.0
        for v in worst.values():
            f *= v
        return max(f, MIN_FACTOR)


def assess_quality(row: pd.Series, sg: pf.ScheduledGame, ctx: pf.UpcomingContext, history: pf.HistoryInputs,
                   pending: list[pf.ScheduledGame], as_of: datetime, box_gaps: dict[int, int]) -> GameQuality:
    q = GameQuality()
    tip = ensure_utc(sg.game_time_utc)
    gp = {"home": int(row["home_gp"]), "away": int(row["away_gp"])}
    q.details["games_played"] = gp
    if min(gp.values()) == 0:
        q.flags.append("season_opener")
    if min(gp.values()) < 5:
        q.flags.append("low_sample")
    if min(gp.values()) < 10:
        q.flags.append("early_season")
    for side, tid, abbr in (("home", sg.home_team_id, sg.home_abbr), ("away", sg.away_team_id, sg.away_abbr)):
        if pd.isna(row.get(f"{side}_ret_min_pct")):
            q.flags.append(f"continuity_unknown:{side}")
        if pd.isna(row.get(f"{side}_est_net_rtg_prev")):
            q.flags.append(f"no_prev_season:{side}")
        if row.get(f"{side}_inj_known") != 1:
            q.flags.append(f"injury_unknown:{side}")
        else:
            st = team_state_asof(history.injury_index, team_abbr=abbr, game_date=et_date(tip), cutoff_utc=as_of)
            unmatched = sum(1 for p in st.players if p.player_id is None)
            no_hist = sum(1 for p in st.players if p.player_id is not None and not ctx.builder.player_hist.get(p.player_id))
            if unmatched:
                q.flags.append(f"injury_unmatched_players:{side}")
                q.details[f"injury_unmatched_{side}"] = unmatched
            if no_hist:
                q.flags.append(f"injury_player_no_history:{side}")
                q.details[f"injury_no_history_{side}"] = no_hist
        dep = row.get(f"{side}_roster_departed_n")
        if dep and not pd.isna(dep) and dep > 0:
            q.flags.append(f"roster_departed:{side}")
            q.details[f"roster_departed_{side}"] = int(dep)
        prior = [p for p in pending if p.game_id != sg.game_id and tid in (p.home_team_id, p.away_team_id)
                 and ensure_utc(p.game_time_utc) < tip]
        if prior:
            q.flags.append(f"pending_prior_game:{side}")
            if any(ensure_utc(p.game_time_utc) < as_of - timedelta(hours=3) for p in prior):
                q.flags.append(f"stale_results:{side}")
        if box_gaps.get(tid, 0) >= 3:
            q.flags.append(f"missing_box_recent:{side}")
            q.details[f"missing_box_recent_{side}"] = box_gaps[tid]
    times = history.injury_index.times
    if not times or times[-1] < as_of - timedelta(hours=24):
        q.flags.append("injury_feed_stale")
    if sg.season_stage not in pf.PHASE_C_STAGES:
        q.flags.append("stage_out_of_training")
    q.flags = sorted(set(q.flags))
    return q


# ------------------------------------------------------------------ #
# 主流程                                                                #
# ------------------------------------------------------------------ #

@dataclass
class GamePrediction:
    game: pf.ScheduledGame
    profile: str
    home_win_prob: float
    pred_margin: float
    pred_total: float
    pred_home_h1: float
    pred_away_h1: float
    pred_home_h2: float
    pred_away_h2: float
    confidence: float
    input_hash: str
    features_json: dict[str, Any]
    quality: GameQuality

    def row_values(self) -> dict[str, float]:
        return {"home_win_prob": self.home_win_prob, "pred_margin": self.pred_margin, "pred_total": self.pred_total,
                "pred_home_h1": self.pred_home_h1, "pred_away_h1": self.pred_away_h1,
                "pred_home_h2": self.pred_home_h2, "pred_away_h2": self.pred_away_h2}


@dataclass
class Skipped:
    game_id: int
    reason: str


def input_hash(art: LoadedArtifact, profile: str, stage: str, X: dict[str, list[float]], flags: list[str]) -> str:
    payload = {"artifact_version": art.artifact_version, "profile": profile, "stage": stage, "flags": flags,
               "x": {t: [round(float(v), 6) for v in xs] for t, xs in sorted(X.items())}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def predict_games(art: LoadedArtifact, history: pf.HistoryInputs, upcoming: list[pf.ScheduledGame],
                  pending: list[pf.ScheduledGame], as_of: datetime, *, kind: str
                  ) -> tuple[list[GamePrediction], list[Skipped]]:
    as_of = ensure_utc(as_of)
    skipped: list[Skipped] = []
    ok: list[pf.ScheduledGame] = []
    for sg in upcoming:
        if ensure_utc(sg.game_time_utc) - as_of < MIN_LEAD:
            skipped.append(Skipped(sg.game_id, "距開賽不足 5 分鐘或已開賽：不產生預測"))
        elif sg.home_team_id is None or sg.away_team_id is None:
            skipped.append(Skipped(sg.game_id, "缺少球隊"))
        else:
            ok.append(sg)
    if not ok:
        return [], skipped
    if not history.games:
        return [], skipped + [Skipped(g.game_id, "沒有任何已完賽歷史：不產生預測") for g in ok]
    ctx = pf.prepare_context(history, as_of)
    box_gaps = team_box_gaps(history)
    by_profile: dict[str, list[pf.ScheduledGame]] = defaultdict(list)
    for sg in ok:
        by_profile[spec.profile_for_lead(ensure_utc(sg.game_time_utc) - as_of)].append(sg)
    results: list[GamePrediction] = []
    for profile, games in by_profile.items():
        prof = art.profile(profile)
        blend = {tuple(k.split("|")): tm.BlendParams(**v) for k, v in prof["blend_params"].items()}
        frame = pf.upcoming_rows(ctx, games, as_of, art.calibrator(profile), pending)
        fin = pf.finalize(frame, blend, prof["fills"])
        preds = predict_profile(prof, fin)
        inputs = {t: model_inputs(prof, fin, t) for t in spec.PRODUCTION_SPEC}
        expl = {t: linear_contributions(prof["models"][t]["estimator"], inputs[t]) for t in spec.PRODUCTION_SPEC}
        by_id = {g.game_id: g for g in games}
        for i, (_, row) in enumerate(fin.iterrows()):
            sg = by_id[int(row["game_id"])]
            q = assess_quality(row, sg, ctx, history, pending, as_of, box_gaps)
            p = float(preds["win"][i])
            margin, total = float(preds["margin"][i]), float(preds["total"][i])
            h1m, h1t = float(preds["h1_margin"][i]), float(preds["h1_total"][i])
            home_pts, away_pts = (total + margin) / 2, (total - margin) / 2
            home_h1, away_h1 = (h1t + h1m) / 2, (h1t - h1m) / 2
            conf_raw = abs(p - 0.5) * 2
            conf = round(conf_raw * q.factor, 4)
            X = {t: inputs[t].iloc[i].tolist() for t in spec.PRODUCTION_SPEC}
            h = input_hash(art, profile, sg.season_stage, X, q.flags)
            fj = _features_json(art, profile, kind, as_of, h, row, sg, q, expl, inputs, i, p, conf_raw,
                                {"margin": margin, "total": total, "h1_margin": h1m, "h1_total": h1t})
            results.append(GamePrediction(sg, profile, p, margin, total, home_h1, away_h1, home_pts - home_h1,
                                          away_pts - away_h1, conf, h, fj, q))
    results.sort(key=lambda r: (r.game.game_time_utc, r.game.game_id))
    return results, skipped


def _features_json(art, profile, kind, as_of, h, row, sg, q, expl, inputs, i, p, conf_raw, reg) -> dict[str, Any]:
    feats_win = art.profile(profile)["models"]["win"]["features"]
    icpt_w, c_w = expl["win"]
    target_contrib = {}
    for t in ("margin", "total", "h1_margin", "h1_total"):
        icpt, c = expl[t]
        target_contrib[t] = {"unit": TARGET_UNITS[t], "method": "ridge：標準化係數 × 標準化特徵值（精確線性分解，非 SHAP）",
                             "intercept": round(icpt, 3), "prediction": round(reg[t], 3),
                             "top": _top(art.profile(profile)["models"][t]["features"], c[i])}

    def inj(side: str) -> dict[str, Any]:
        return {"known": row.get(f"{side}_inj_known") == 1,
                "report_lead_h": _num(row.get(f"{side}_inj_report_lead_h"), 2),
                "n_out": _num(row.get(f"{side}_inj_n_out"), 0), "n_doubtful": _num(row.get(f"{side}_inj_n_doubtful"), 0),
                "n_questionable": _num(row.get(f"{side}_inj_n_questionable"), 0),
                "min_lost_role": _num(row.get(f"{side}_inj_min_lost_role"), 1),
                "exp_starters_avail": _num(row.get(f"{side}_inj_exp_starters_avail"), 2)}

    gp_h, gp_a = int(row["home_gp"]), int(row["away_gp"])
    return {
        "model_version": art.model_version, "artifact_version": art.artifact_version,
        "feature_version": art.bundle["feature_version"], "training_cutoff_utc": art.training_cutoff_utc,
        "prediction_kind": kind, "prediction_as_of_utc": as_of.isoformat(), "profile": profile,
        "injury_timing": spec.PROFILES[profile], "input_hash": h,
        "win_model": "logistic", "contributions_unit": "log-odds",
        "contributions_note": "正值 = 有利主隊；標準化係數 × 標準化特徵值（精確線性分解），Σ + 截距 = 預測 logit",
        "contributions": _top(feats_win, c_w[i]), "win_logit_intercept": round(icpt_w, 4),
        "target_contributions": target_contrib,
        "elo_home": _num(row.get("elo_home"), 1), "elo_away": _num(row.get("elo_away"), 1),
        "rest_days_home": _num(row.get("home_rest_days_v2"), 2), "rest_days_away": _num(row.get("away_rest_days_v2"), 2),
        "form_home_last10": _num(row.get("home_form10")), "form_away_last10": _num(row.get("away_form10")),
        "games_played_home": gp_h, "games_played_away": gp_a,
        "early_season": min(gp_h, gp_a) < 10, "low_sample": min(gp_h, gp_a) < 5,
        "continuity_known": bool(pd.notna(row.get("home_ret_min_pct")) and pd.notna(row.get("away_ret_min_pct"))),
        "season_stage": sg.season_stage,
        "injuries": {"home": inj("home"), "away": inj("away")},
        "data_quality": {"flags": q.flags, "details": q.details, "confidence_factor": round(q.factor, 4),
                         "prediction_allowed": True},
        "confidence_raw": round(conf_raw, 4),
    }
