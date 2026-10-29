"""唯讀前瞻模型評估；只評分當時已寫入的賽前預測，不重建或調整模型。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
import math

import numpy as np

from ..timeutil import ET, ensure_utc, parse_utc
from . import spec

REPORT_VERSION = "prospective-monitor-v1"
TARGETS = ("margin", "total", "h1_margin", "h1_total")


def _timestamp(value):
    try:
        return parse_utc(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def select_predictions(games, predictions, checked_at, mode="final_t60"):
    """每場一筆；同時檢查寫入 / 推論 / 訓練時間。排除事後回填及無法證明來源者。

    early = 最早可用的 early profile（包含 refresh，避免以 job kind 混淆 profile）；
    final_t60 = T-60 前已寫入的最後一筆 final profile；latest = 開賽前最後一筆。
    """
    if mode not in ("early", "final_t60", "latest"):
        raise ValueError("unknown selection mode")
    checked_at = ensure_utc(checked_at)
    game_map = {g["game_id"]: g for g in games}
    candidates = defaultdict(list)
    excluded = Counter()
    for p in predictions:
        g = game_map.get(p.get("game_id"))
        if g is None or p.get("model_version") != spec.MODEL_VERSION:
            excluded["outside_cohort_or_model"] += 1
            continue
        f = p.get("features_json")
        f = f if isinstance(f, dict) else {}
        created, as_of, trained = (_timestamp(p.get("created_at")),
                                  _timestamp(f.get("prediction_as_of_utc")),
                                  _timestamp(f.get("training_cutoff_utc")))
        tip = _timestamp(g.get("date_utc"))
        if (not all((created, as_of, trained, tip)) or not f.get("artifact_version")
                or f.get("model_version") != spec.MODEL_VERSION
                or f.get("profile") not in spec.PROFILES):
            excluded["missing_or_invalid_provenance"] += 1
            continue
        if trained > as_of or as_of > created or max(created, as_of) >= tip or created > checked_at:
            excluded["invalid_time_order_or_post_tip"] += 1
            continue
        deadline = tip - timedelta(minutes=60) if mode == "final_t60" else tip
        if max(created, as_of) > deadline:
            excluded["outside_decision_window"] += 1
            continue
        if mode != "latest" and f["profile"] != ("final" if mode == "final_t60" else "early"):
            excluded["other_profile"] += 1
            continue
        candidates[g["game_id"]].append(p)
    selected = []
    for g in games:
        if g["game_id"] in candidates:
            choose = min if mode == "early" else max
            p = choose(candidates[g["game_id"]], key=lambda p: (_timestamp(p["created_at"]), p["id"]))
            selected.append((g, p))
    return selected, dict(excluded)


def _wilson(hits, n):
    if not n:
        return None
    z = 1.959963984540054
    rate, den = hits / n, 1 + z * z / n
    center = (rate + z * z / (2 * n)) / den
    radius = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / den
    return [center - radius, center + radius]


def _day_bootstrap_ci(values, days):
    """按 ET 比賽日整塊抽樣；非獨立場次假設，固定 seed，2,000 次。"""
    groups = defaultdict(list)
    for day, v in zip(days, values):
        groups[day].append(v)
    if len(groups) < 2:
        return None
    sums = np.array([sum(v) for _, v in sorted(groups.items())])
    counts = np.array([len(v) for _, v in sorted(groups.items())])
    indices = np.random.default_rng(20261029).integers(0, len(groups), (2000, len(groups)))
    means = sums[indices].sum(axis=1) / counts[indices].sum(axis=1)
    return np.quantile(means, [.025, .975]).tolist()


def _win_metrics(rows):
    pairs = [(g, p) for g, p in rows if _finite(p.get("home_win_prob")) and 0 < p["home_win_prob"] < 1]
    if not pairs:
        return {"n": 0}
    prob = np.array([p["home_win_prob"] for g, p in pairs])
    y = np.array([g["home_pts"] > g["away_pts"] for g, p in pairs], dtype=float)
    hits = int(((prob >= .5) == y).sum())
    bins = []
    for lo, hi in ((0, .4), (.4, .5), (.5, .6), (.6, .7), (.7, 1)):
        mask = (prob >= lo) & (prob < hi)
        if mask.any():
            bins.append({"range": [lo, hi], "n": int(mask.sum()), "mean_probability": float(prob[mask].mean()),
                         "home_win_rate": float(y[mask].mean())})
    return {"n": len(pairs), "hits": hits, "accuracy": hits / len(pairs),
            "accuracy_wilson_95": _wilson(hits, len(pairs)),
            "log_loss": float(-(y * np.log(prob) + (1 - y) * np.log(1 - prob)).mean()),
            "brier": float(((prob - y) ** 2).mean()), "mean_home_probability": float(prob.mean()),
            "always_home_accuracy": float(y.mean()), "constant_50_log_loss": math.log(2),
            "constant_50_brier": .25, "calibration_bins": bins}


def _regression_metrics(rows, target):
    values, preds, days, covered = [], [], [], {80: [], 95: []}
    for g, p in rows:
        half = target.startswith("h1_")
        h, a = (g.get("home_h1"), g.get("away_h1")) if half else (g.get("home_pts"), g.get("away_pts"))
        ph, pa = p.get("pred_home_h1"), p.get("pred_away_h1")
        if not all(_finite(v) for v in (h, a)) or (half and not all(_finite(v) for v in (ph, pa))):
            continue
        sign = -1 if "margin" in target else 1
        pred = ph + sign * pa if half else p.get("pred_" + target)
        if not _finite(pred):
            continue
        actual = h + sign * a
        values.append(actual - pred)
        preds.append(pred)
        days.append(_timestamp(g["date_utc"]).astimezone(ET).date().isoformat())
        summary = p["features_json"].get("predictive_distributions", {}).get(target, {})
        for level in covered:
            interval = summary.get(f"interval_{level}")
            if (isinstance(interval, list) and len(interval) == 2
                    and all(_finite(v) for v in interval) and interval[0] <= interval[1]):
                covered[level].append(interval[0] <= actual <= interval[1])
    if not values:
        return {"n": 0}
    residual = np.array(values)
    return {"n": len(values), "mae": float(np.abs(residual).mean()),
            "rmse": float(np.sqrt((residual ** 2).mean())), "bias_actual_minus_prediction": float(residual.mean()),
            "bias_et_day_bootstrap_95": _day_bootstrap_ci(values, days),
            "mae_et_day_bootstrap_95": _day_bootstrap_ci(np.abs(residual).tolist(), days),
            "mean_prediction": float(np.mean(preds)), "mean_actual": float(np.mean(preds) + residual.mean()),
            "interval_coverage": {str(k): {"n": len(v), "coverage": float(np.mean(v)) if v else None}
                                  for k, v in covered.items()}}


def score_rows(rows):
    return {"n_games": len(rows), "win_logistic": _win_metrics(rows),
            **{t: _regression_metrics(rows, t) for t in TARGETS}}


def evaluate_snapshot(snapshot):
    checked = _timestamp(snapshot["checked_at"])
    if checked is None:
        raise ValueError("snapshot checked_at is required")
    games = [g for g in snapshot["games"] if g.get("status") == "final"
             and g.get("season_stage") in ("regular", "playin", "playoffs")
             and _timestamp(g.get("date_utc")) and _timestamp(g["date_utc"]) < checked
             and all(_finite(g.get(k)) for k in ("home_pts", "away_pts"))
             and g["home_pts"] != g["away_pts"]]
    out = {"report_version": REPORT_VERSION, "checked_at": checked.isoformat(),
           "evidence_type": "observed_prospective_predictions_current_results",
           "model_version": spec.MODEL_VERSION, "n_final_games": len(games),
           "game_range_utc": [min(g["date_utc"] for g in games), max(g["date_utc"] for g in games)] if games else None,
           "notes": ["win_logistic 是網站勝率；台彩定價優先採用分差分佈，兩者不可混為一談。",
                     "每場每個 profile 只評分一筆；early / final 不合併成獨立樣本。",
                     "early 是最早可用的 early profile，未必剛好在 ET 00:00；不能直接與歷史固定時點結果作成對比較。",
                     "以當前已結算比分核對；無法重建當時比分入庫時間或驗證遠端 artifact 檔案。",
                     "區塊 bootstrap 僅供診斷，少量比賽日的區間不穩定；不是模型調整或 promotion gate。"],
           "evaluations": {}}
    for mode in ("early", "final_t60", "latest"):
        rows, excluded = select_predictions(games, snapshot["predictions"], checked, mode)
        by_artifact = defaultdict(list)
        for g, p in rows:
            by_artifact[p["features_json"]["artifact_version"]].append((g, p))
        out["evaluations"][mode] = {
            **score_rows(rows), "coverage": len(rows) / len(games) if games else None,
            "missing_game_ids": [g["game_id"] for g in games if g["game_id"] not in {r[0]["game_id"] for r in rows}],
            "excluded_prediction_rows": excluded,
            "quality_flags": dict(Counter(f for g, p in rows for f in p["features_json"].get("data_quality", {}).get("flags", []))),
            "by_artifact": {v: score_rows(rr) for v, rr in sorted(by_artifact.items())}}
    # 附帶原始彙總供人工檢視；不由缺漏盤口 / 空 paper ledger 推算 ROI。
    for key in ("window_utc", "odds", "paper", "paper_bets", "health"):
        out[key] = snapshot.get(key, [])
    out["adaptation"] = {"status": "REVIEW_ONLY" if games else "WAITING", "production_changed": False,
                         "recommendation": "保留既有特徵更新與固定規格週重訓；新校正須另版本並以前瞻資料驗證。"}
    return out


def fetch_snapshot(days=14):
    """直接連線並驗證 READ ONLY；不使用 pooler 可能忽略的 startup options。"""
    import psycopg
    from psycopg.rows import dict_row
    from ..config import settings

    if not 1 <= days <= 366:
        raise ValueError("days must be between 1 and 366")
    with psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=15) as conn:
        conn.read_only = True
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            if cur.fetchone()["transaction_read_only"] != "on":
                raise RuntimeError("read-only transaction required")
            cur.execute("SET LOCAL statement_timeout = '30s'")
            cur.execute("SELECT now() AS checked_at")
            out = cur.fetchone()
            end = out["checked_at"]
            start = end - timedelta(days=days)
            out["window_utc"] = [start.isoformat(), end.isoformat()]
            cur.execute("""SELECT id AS game_id, season, season_stage, date_utc, status,
                home_pts,away_pts,home_h1,away_h1 FROM games
                WHERE date_utc >= %s AND date_utc <= %s ORDER BY date_utc,id""", (start, end))
            out["games"] = cur.fetchall()
            cur.execute("""SELECT p.id,p.game_id,p.model_version,p.created_at,p.home_win_prob,
                p.pred_margin,p.pred_total,p.pred_home_h1,p.pred_away_h1,p.features_json
                FROM predictions p JOIN games g ON g.id=p.game_id
                WHERE g.date_utc >= %s AND g.date_utc <= %s AND p.model_version=%s
                ORDER BY p.created_at,p.id""", (start, end, spec.MODEL_VERSION))
            out["predictions"] = cur.fetchall()
            cur.execute("""SELECT source,count(*) n,count(DISTINCT o.game_id) games,
                min(fetched_at) first_fetched_at,max(fetched_at) last_fetched_at
                FROM odds_snapshots o JOIN games g ON g.id=o.game_id
                WHERE g.date_utc >= %s AND g.date_utc <= %s GROUP BY source""", (start, end))
            out["odds"] = cur.fetchall()
            cur.execute("""SELECT source,decision_status,no_bet_reason,count(*) n
                FROM paper_strategy_decisions WHERE scheduled_tipoff >= %s AND scheduled_tipoff <= %s
                GROUP BY 1,2,3 ORDER BY 1,2,3""", (start, end))
            out["paper"] = cur.fetchall()
            cur.execute("""SELECT source,settlement_status,count(*) n,sum(stake_units) stake_units,
                sum(profit_units) profit_units FROM paper_strategy_bets
                WHERE scheduled_tipoff >= %s AND scheduled_tipoff <= %s GROUP BY 1,2 ORDER BY 1,2""", (start, end))
            out["paper_bets"] = cur.fetchall()
            cur.execute("""SELECT source_key,last_status,last_success_at,records_updated FROM data_sources
                WHERE source_key IN ('model_predict','model_retrain','nba_cdn')""")
            out["health"] = cur.fetchall()
    return out
