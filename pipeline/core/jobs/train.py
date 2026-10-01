"""
Phase C：勝負(邏輯迴歸) + XGBoost 分差 / 總分 + 上半場模型，walk-forward 評測
------------------------------------------------------------
- 以賽季為單位的 walk-forward：評測賽季 S 只用 S 之前的賽季訓練，嚴禁隨機切分。
- 勝負模型：邏輯迴歸（5 個特徵，天然校準）。實測 XGBoost 在驗證賽季上全面輸給它
  （log loss 0.638 vs 0.628），故勝負用邏輯迴歸；分差/總分/半場仍用 XGBoost。
  特徵與 C 值僅以評測賽季之前的賽季（2022-23、2023-24）選出。
- 與 Elo baseline 在「同一批評測比賽」上比較（勝負：accuracy/log loss/Brier；
  分差/總分：MAE，baseline 見 BASELINES 說明）。
- 評測賽季逐場預測寫入 predictions（model_version=xgb-v1.0），指標寫入 model_metrics。
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error

from ..db import cursor, heartbeat
from ..models.ml_features import FEATURE_COLUMNS, FEATURE_LABELS, WIN_FEATURES, build_features, load_games

log = logging.getLogger(__name__)

ML_VERSION = "ml-v1.0"
ELO_BASELINE_VERSION = "elo-v1.0"
ARTIFACT_DIR = Path(__file__).resolve().parents[2] / "artifacts"

COMMON = dict(n_estimators=300, learning_rate=0.03, max_depth=3, subsample=0.8,
              colsample_bytree=0.8, min_child_weight=20, reg_lambda=5.0,
              tree_method="hist", n_jobs=4, random_state=42)

REG_TARGETS = ["margin", "total", "h1_margin", "h1_total"]


def _reg() -> xgb.XGBRegressor:
    return xgb.XGBRegressor(objective="reg:squarederror", **COMMON)


def _fit_win_model(train: pd.DataFrame):
    m = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=1000))
    return m.fit(train[WIN_FEATURES].fillna(0), train["win"])


def _win_metrics(p: np.ndarray, y: np.ndarray) -> dict[str, float]:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return {"accuracy": float(((p > 0.5) == (y == 1)).mean()),
            "log_loss": float(log_loss(y, p)), "brier": float(brier_score_loss(y, p))}


def _sign_acc(pred: np.ndarray, actual: np.ndarray) -> float:
    mask = actual != 0
    return float((np.sign(pred[mask]) == np.sign(actual[mask])).mean())


def _load_elo_baseline_probs() -> dict[int, float]:
    with cursor() as cur:
        cur.execute("SELECT game_id, home_win_prob FROM predictions WHERE model_version = %s",
                    (ELO_BASELINE_VERSION,))
        return {r["game_id"]: float(r["home_win_prob"]) for r in cur.fetchall()}


def evaluate_season(feats: pd.DataFrame, season: str, elo_probs: dict[int, float]):
    train = feats[feats["season"] < season].dropna(subset=["elo_diff"])
    test = feats[feats["season"] == season].dropna(subset=["elo_diff"]).copy()
    clf = _fit_win_model(train)
    test["p_home"] = clf.predict_proba(test[WIN_FEATURES].fillna(0))[:, 1]

    models = {}
    for t in REG_TARGETS:
        tr = train.dropna(subset=[t])
        models[t] = _reg().fit(tr[FEATURE_COLUMNS], tr[t])
        test[f"pred_{t}"] = models[t].predict(test[FEATURE_COLUMNS])

    # ---- baselines ----
    test["elo_p"] = test["game_id"].map(elo_probs)
    lin_margin = LinearRegression().fit(train[["elo_diff"]], train["margin"])
    lin_h1 = LinearRegression().fit(train.dropna(subset=["h1_margin"])[["elo_diff"]],
                                    train.dropna(subset=["h1_margin"])["h1_margin"])
    test["base_margin"] = lin_margin.predict(test[["elo_diff"]])
    test["base_h1_margin"] = lin_h1.predict(test[["elo_diff"]])
    test["base_total"] = test["total_est"].fillna(train["total"].mean())
    test["base_h1_total"] = test["h1_total_est"].fillna(train["h1_total"].mean())
    return test, clf, models, train


def summarize(test: pd.DataFrame) -> dict[str, Any]:
    y = test["win"].values
    out: dict[str, Any] = {"n": len(test)}
    out["model"] = _win_metrics(test["p_home"].values, y)
    has_elo = test["elo_p"].notna()
    out["elo"] = _win_metrics(test.loc[has_elo, "elo_p"].values, y[has_elo.values])
    out["model_on_elo_games"] = _win_metrics(test.loc[has_elo, "p_home"].values, y[has_elo.values])
    for t in REG_TARGETS:
        m = test[t].notna()
        out[f"mae_{t}"] = float(mean_absolute_error(test.loc[m, t], test.loc[m, f"pred_{t}"]))
        out[f"mae_{t}_base"] = float(mean_absolute_error(test.loc[m, t], test.loc[m, f"base_{t}"]))
    m = test["h1_margin"].notna()
    out["h1_accuracy"] = _sign_acc(test.loc[m, "pred_h1_margin"].values, test.loc[m, "h1_margin"].values)
    out["h1_accuracy_base"] = _sign_acc(test.loc[m, "base_h1_margin"].values, test.loc[m, "h1_margin"].values)
    return out


def _contributions(model, rows: pd.DataFrame, top: int = 6) -> list[list[dict]]:
    """邏輯迴歸的精確貢獻（對數勝率）：coef × 標準化後的特徵值。"""
    scaler, lr = model.steps[0][1], model.steps[1][1]
    z = scaler.transform(rows[WIN_FEATURES].fillna(0))
    contrib = z * lr.coef_[0]
    out = []
    for c in contrib:
        order = np.argsort(-np.abs(c))[:top]
        out.append([{"label": FEATURE_LABELS[WIN_FEATURES[i]], "value": round(float(c[i]), 3)}
                    for i in order])
    return out


def _num(v: Any) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 3)


def persist_metrics(season_label: str, s: dict[str, Any], note: str) -> None:
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO model_metrics
              (model_version, season, evaluated_at, n_games, accuracy, h1_accuracy, log_loss,
               brier, mae_margin, mae_total, notes)
            VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (ML_VERSION, season_label, s["n"], s["model"]["accuracy"], s["h1_accuracy"],
             s["model"]["log_loss"], s["model"]["brier"], s["mae_margin"], s["mae_total"], note),
        )


def save_final_artifact(feats: pd.DataFrame) -> Path:
    """用全部賽季訓練並存檔，供之後對未來賽事做即時預測。"""
    train = feats.dropna(subset=["elo_diff"])
    regs = {t: _reg().fit(train.dropna(subset=[t])[FEATURE_COLUMNS], train.dropna(subset=[t])[t])
            for t in REG_TARGETS}
    ARTIFACT_DIR.mkdir(exist_ok=True)
    path = ARTIFACT_DIR / f"{ML_VERSION}.joblib"
    joblib.dump({"win": _fit_win_model(train), "win_features": WIN_FEATURES,
                 "regs": regs, "reg_features": FEATURE_COLUMNS}, path)
    return path


def run(eval_seasons: int = 2, write: bool = True) -> dict[str, Any]:
    with cursor() as cur:
        feats = build_features(load_games(cur))
    seasons = sorted(feats["season"].unique())
    evals = seasons[-eval_seasons:]
    log.info("特徵 %d 場，賽季 %s，評測：%s", len(feats), seasons, evals)

    elo_probs = _load_elo_baseline_probs()
    tests, clfs, results = [], [], {}
    for s in evals:
        test, clf, *_ = evaluate_season(feats, s, elo_probs)
        results[s] = summarize(test)
        tests.append(test)
        clfs.append(clf)  # 該賽季之前訓練的分類器，用於該賽季的 SHAP 貢獻
    all_test = pd.concat(tests)
    results["all"] = summarize(all_test)

    if write:
        with cursor() as cur:
            cur.execute("DELETE FROM predictions WHERE model_version = %s", (ML_VERSION,))
        n = 0
        for test, clf in zip(tests, clfs):
            _insert_rows(test, _contributions(clf, test))
            n += len(test)
        note = f"walk-forward(賽季為單位)；特徵：比賽層級(Elo/休息/近況/交手/賽程)；暖身/訓練：{seasons[:-eval_seasons]}"
        for s in evals:
            persist_metrics(s, results[s], note)
        persist_metrics(",".join(evals), results["all"], note)
        path = save_final_artifact(feats)
        with cursor() as cur:
            heartbeat(cur, source_key="model_predict", display_name="預測引擎 (XGBoost)",
                      category="model", status="ok", records_updated=n)
        log.info("寫入 %d 筆 predictions；模型存檔 %s", n, path)
    return results


def _insert_rows(test: pd.DataFrame, contribs: list[list[dict]]) -> None:
    rows = []
    for (_, r), contrib in zip(test.iterrows(), contribs):
        home_pts = (r["pred_total"] + r["pred_margin"]) / 2
        home_h1 = (r["pred_h1_total"] + r["pred_h1_margin"]) / 2
        away_pts, away_h1 = r["pred_total"] - home_pts, r["pred_h1_total"] - home_h1
        fj = {
            "elo_home": _num(r["elo_home"]), "elo_away": _num(r["elo_away"]),
            "rest_days_home": _num(r["home_rest_days"]), "rest_days_away": _num(r["away_rest_days"]),
            "form_home_last10": _num(r["home_form10"]), "form_away_last10": _num(r["away_form10"]),
            "h2h_home_win5": _num(r["h2h_home_win5"]),
            "elo_baseline_prob": _num(r["elo_p"]), "win_model": "logistic",
            "contributions_unit": "log-odds", "contributions": contrib,
        }
        rows.append((
            int(r["game_id"]), ML_VERSION, float(r["p_home"]), float(r["pred_margin"]),
            float(r["pred_total"]), float(home_h1), float(away_h1),
            float(home_pts - home_h1), float(away_pts - away_h1),
            round(abs(float(r["p_home"]) - 0.5) * 2, 4), json.dumps(fj, ensure_ascii=False),
        ))
    with cursor() as cur:
        cur.executemany(
            """
            INSERT INTO predictions
              (game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
               pred_home_h1, pred_away_h1, pred_home_h2, pred_away_h2, confidence, features_json)
            VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            rows,
        )


def _print(results: dict[str, Any]) -> None:
    for k, s in results.items():
        print(f"\n=== {k}  (n={s['n']}) ===")
        for name in ("elo", "model_on_elo_games", "model"):
            m = s[name]
            print(f"  勝負 {name:18s} acc={m['accuracy']:.4f} logloss={m['log_loss']:.4f} brier={m['brier']:.4f}")
        for t in REG_TARGETS:
            print(f"  MAE {t:10s} xgb={s[f'mae_{t}']:.3f}  baseline={s[f'mae_{t}_base']:.3f}")
        print(f"  上半場勝負方向 xgb={s['h1_accuracy']:.4f} baseline={s['h1_accuracy_base']:.4f}")


def main() -> None:
    from ..logging_conf import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser(description="XGBoost walk-forward 訓練與評測")
    ap.add_argument("--eval-seasons", type=int, default=2)
    ap.add_argument("--no-write", action="store_true", help="只評測，不寫入資料庫/模型檔")
    args = ap.parse_args()
    _print(run(args.eval_seasons, write=not args.no_write))


if __name__ == "__main__":
    main()
