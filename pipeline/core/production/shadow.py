"""adaptive-shadow-v1: append-only prospective pairs; never used by pricing or production inference."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json
import math
import os
from uuid import uuid4

from ..timeutil import ET, TPE, ensure_utc, parse_utc
from .monitoring import TARGETS, _day_bootstrap_ci, _finite, score_rows, select_predictions

VERSION = "adaptive-shadow-v1"
LOCK_KEY = "adaptive-shadow-v1-ledger"
# Chosen for a conservative new experiment, not fitted to the reviewed 68 games.
SPEC = {"version": VERSION, "prior_games": 200, "max_shift_points": 2.0,
        "baseline_model_version": "ml-v2.0", "baseline_distribution_version": "dist-v2",
        "primary_metric": "paired total MAE on adjusted prospective pairs",
        "min_training_games": 30, "min_training_et_days": 7, "lookback_days": 90,
        "update": "Monday 16:10 Asia/Taipei; actual observation time, no backdating",
        "capture": "first valid final profile observed between T-75 and T-60",
        "targets": list(TARGETS), "logistic_unchanged": True,
        "review_min_adjusted_pairs": 300, "review_min_et_days": 28, "review_min_elapsed_days": 42,
        "automatic_promotion": False, "historical_bootstrap": False}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def points(p):
    return {"margin": p["pred_margin"], "total": p["pred_total"],
            "h1_margin": p["pred_home_h1"] - p["pred_away_h1"],
            "h1_total": p["pred_home_h1"] + p["pred_away_h1"]}


def actuals(g):
    out = {"margin": g["home_pts"] - g["away_pts"], "total": g["home_pts"] + g["away_pts"]}
    if all(_finite(g.get(k)) for k in ("home_h1", "away_h1")):
        out.update(h1_margin=g["home_h1"]-g["away_h1"], h1_total=g["home_h1"]+g["away_h1"])
    return out


def fit_state(pairs, as_of):
    """Only ledger results already observed before fitting; missing halves excluded per target."""
    as_of = ensure_utc(as_of)
    residuals = {t: [] for t in TARGETS}
    days = {t: set() for t in TARGETS}
    used = []
    for pair in pairs:
        g, pred = pair["result"], pair["prediction"]
        observed = parse_utc(pair["observed_at"])
        tip = parse_utc(g["date_utc"])
        if (g.get("status") != "final" or not (as_of - timedelta(days=SPEC["lookback_days"]) < tip < observed < as_of)
                or not all(_finite(g.get(k)) for k in ("home_pts", "away_pts"))):
            continue
        pp = points(pred)
        for target, y in actuals(g).items():
            residuals[target].append(y - pp[target])
            days[target].add(tip.astimezone(ET).date())
        used.append(pair["result_id"])
    shifts = {}
    for t, rr in residuals.items():
        ready = len(rr) >= SPEC["min_training_games"] and len(days[t]) >= SPEC["min_training_et_days"]
        shifts[t] = max(-SPEC["max_shift_points"], min(SPEC["max_shift_points"],
                         sum(rr)/(len(rr)+SPEC["prior_games"]))) if ready else 0.0
    return {"shifts": shifts, "n_by_target": {t: len(v) for t, v in residuals.items()},
            "et_days_by_target": {t: len(v) for t, v in days.items()}, "training_result_ids": sorted(used),
            "fit_as_of": as_of.isoformat()}


def shifted_prediction(baseline, shifts, art):
    """Shift point centers only; reuse the exact baseline artifact's distribution implementation."""
    from .inference import _distribution_summary
    from .probability import line_probability_from_prediction_row
    p = deepcopy(baseline)
    v = {t: float(x) + shifts[t] for t, x in points(p).items()}
    if not all(math.isfinite(x) for x in v.values()) or any(abs(x) > SPEC["max_shift_points"] for x in shifts.values()):
        raise ValueError("invalid shadow shift")
    p.update(pred_margin=v["margin"], pred_total=v["total"],
             pred_home_h1=(v["h1_total"]+v["h1_margin"])/2, pred_away_h1=(v["h1_total"]-v["h1_margin"])/2)
    fj = p["features_json"]
    fj["predictive_distributions"] = _distribution_summary(art, fj["profile"], v, fj["distribution_context"])
    p["margin_home_win_prob"] = line_probability_from_prediction_row(p, "margin", 0, art=art)["probability_above"]
    return p


def schema_ready(cur):
    cur.execute("SELECT to_regclass('model_shadow_experiments') IS NOT NULL AS ok")
    return cur.fetchone()["ok"]


def _clock(cur):
    cur.execute("SELECT clock_timestamp() AS now")
    return cur.fetchone()["now"]


def _experiment(cur, activate=False):
    cur.execute("SELECT * FROM model_shadow_experiments WHERE version=%s", (VERSION,))
    row = cur.fetchone()
    if row is None and activate:
        cur.execute("INSERT INTO model_shadow_experiments VALUES (%s,clock_timestamp(),%s) RETURNING *", (VERSION, dumps(SPEC)))
        row = cur.fetchone()
    if row and json.loads(row["spec_json"]) != SPEC:
        raise ValueError("shadow spec changed; a new experiment version is required")
    return row


def _pairs(cur, as_of):
    cur.execute("""SELECT p.*, r.id AS result_id,r.observed_at,r.payload AS result_payload
        FROM model_shadow_predictions p JOIN LATERAL (
          SELECT * FROM model_shadow_results WHERE shadow_prediction_id=p.id AND observed_at < %s
          ORDER BY observed_at DESC,id DESC LIMIT 1) r ON true
        WHERE p.experiment_version=%s ORDER BY p.scheduled_tipoff,p.id""", (as_of, VERSION))
    out = []
    for row in cur.fetchall():
        payload = json.loads(row["payload"])
        result = json.loads(row["result_payload"])
        if result.get("status") == "final":
            out.append({**payload, "result": result, "observed_at": row["observed_at"], "result_id": row["result_id"],
                        "prediction": payload["baseline"]})
    return out


def _observe_results(cur):
    now = _clock(cur)
    cur.execute("""SELECT p.id,p.scheduled_tipoff,g.id AS game_id,g.date_utc,g.status,g.home_pts,g.away_pts,
        g.home_h1,g.away_h1,previous.payload AS previous_payload
        FROM model_shadow_predictions p JOIN games g ON g.id=p.game_id
        LEFT JOIN LATERAL (SELECT payload FROM model_shadow_results WHERE shadow_prediction_id=p.id
            ORDER BY observed_at DESC,id DESC LIMIT 1) previous ON true
        WHERE p.experiment_version=%s AND (g.status='final' OR g.date_utc<>p.scheduled_tipoff
          OR previous.payload IS NOT NULL)""", (VERSION,))
    n = 0
    for row in cur.fetchall():
        if row["status"] == "final" and row["date_utc"] >= now:
            continue
        result = {k: row[k] for k in ("game_id", "date_utc", "status", "home_pts", "away_pts", "home_h1", "away_h1")}
        if row["date_utc"] != row["scheduled_tipoff"]:
            result["status"] = "invalidated_schedule"
        elif row["status"] == "final" and (not all(_finite(row[k]) for k in ("home_pts", "away_pts")) or row["home_pts"] == row["away_pts"]):
            result["status"] = "ungradable_scores"
        body = dumps(result)
        if row["previous_payload"] != body:
            cur.execute("INSERT INTO model_shadow_results VALUES (%s,%s,clock_timestamp(),%s)", (str(uuid4()), row["id"], body))
            n += 1
    return n


def _state(cur, experiment):
    now = _clock(cur)
    local = now.astimezone(TPE)
    monday = (local - timedelta(days=local.weekday())).replace(hour=16, minute=10, second=0, microsecond=0)
    if local < monday:
        monday -= timedelta(days=7)
    period = monday.isoformat() if local >= monday and monday > experiment["activated_at"].astimezone(TPE) else "initial"
    cur.execute("SELECT * FROM model_shadow_states WHERE experiment_version=%s AND period_key=%s", (VERSION, period))
    state = cur.fetchone()
    if state is None:
        fitted = fit_state(_pairs(cur, now), now)
        cur.execute("INSERT INTO model_shadow_states VALUES (%s,%s,%s,clock_timestamp(),%s) RETURNING *",
                    (str(uuid4()), VERSION, period, dumps(fitted)))
        state = cur.fetchone()
    return state


def collect(activate=False, root=None):
    from ..db import transaction
    from .artifact import load_version
    from .probability import line_probability_from_prediction_row

    with transaction() as cur:
        if not schema_ready(cur):
            return {"status": "WAITING_SCHEMA"}
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok", (LOCK_KEY,))
        if not cur.fetchone()["ok"]:
            return {"status": "BUSY"}
        experiment = _experiment(cur, activate)
        if experiment is None:
            return {"status": "NOT_ACTIVATED"}
        observed = _observe_results(cur)
        state = _state(cur, experiment)
        now = _clock(cur)
        cur.execute("""SELECT id AS game_id,date_utc,status,season_stage FROM games g
          WHERE status='scheduled' AND season_stage IN ('regular','playin','playoffs')
          AND date_utc BETWEEN %s AND %s AND date_utc>%s
          AND NOT EXISTS (SELECT 1 FROM model_shadow_predictions p WHERE p.game_id=g.id AND p.experiment_version=%s)
          ORDER BY date_utc,id""", (now+timedelta(minutes=60), now+timedelta(minutes=75), experiment["activated_at"], VERSION))
        games = cur.fetchall()
        inserted = 0
        shifts = json.loads(state["payload"])["shifts"]
        for game in games:
            cur.execute("SELECT * FROM predictions WHERE game_id=%s AND model_version='ml-v2.0' ORDER BY created_at,id", (game["game_id"],))
            selected, _ = select_predictions([game], cur.fetchall(), now, "final_t60")
            if not selected:
                continue
            baseline = selected[0][1]
            art = load_version(baseline["features_json"]["artifact_version"], root)
            baseline["margin_home_win_prob"] = line_probability_from_prediction_row(baseline, "margin", 0, art=art)["probability_above"]
            candidate = shifted_prediction(baseline, shifts, art)
            body = dumps({"baseline": baseline, "candidate": candidate, "shifts": shifts})
            # Recheck with server clock after artifact loading. Never backfill a missed T-60 window.
            cur.execute("""INSERT INTO model_shadow_predictions
              SELECT %s,%s,%s,%s,%s,%s,clock_timestamp(),%s
              WHERE clock_timestamp()<=%s AND clock_timestamp()>=%s
              ON CONFLICT (experiment_version,game_id) DO NOTHING""",
              (str(uuid4()), VERSION, game["game_id"], baseline["id"], state["id"], game["date_utc"], body,
               game["date_utc"]-timedelta(minutes=60), state["effective_at"]))
            inserted += cur.rowcount
        return {"status": "COLLECTING", "captured": inserted, "results_observed": observed, "state_id": state["id"]}


def summary(cur, now):
    experiment = _experiment(cur)
    if experiment is None:
        return {"version": VERSION, "status": "NOT_ACTIVATED", "n_pairs": 0}
    pairs = _pairs(cur, now)
    cur.execute("SELECT count(*) AS n FROM model_shadow_predictions WHERE experiment_version=%s", (VERSION,))
    n_captured = cur.fetchone()["n"]
    cur.execute("""SELECT count(*) AS n FROM games WHERE date_utc>%s AND date_utc<%s
        AND season_stage IN ('regular','playin','playoffs') AND status='final'""", (experiment["activated_at"], now))
    n_final = cur.fetchone()["n"]
    baseline = [(p["result"], p["baseline"]) for p in pairs]
    candidate = [(p["result"], p["candidate"]) for p in pairs]
    adjusted = [p for p in pairs if any(abs(x)>0 for x in p["shifts"].values())]
    days = {parse_utc(p["result"]["date_utc"]).astimezone(ET).date() for p in adjusted}
    ready = (len(adjusted)>=SPEC["review_min_adjusted_pairs"] and len(days)>=SPEC["review_min_et_days"]
             and now-experiment["activated_at"]>=timedelta(days=SPEC["review_min_elapsed_days"]))
    out = {"version": VERSION, "status": "READY_FOR_REVIEW" if ready else "COLLECTING",
           "collector_enabled": os.getenv("MODEL_SHADOW_ENABLED", "").lower() in ("1", "true"),
           "activated_at": experiment["activated_at"], "n_captured": n_captured, "n_pairs": len(pairs),
           "n_final_since_activation": n_final,
           "n_adjusted_pairs": len(adjusted), "baseline": score_rows(baseline), "candidate": score_rows(candidate),
           "logistic_unchanged": True, "automatic_promotion": False, "paired_mae_delta": {}}
    for side, rows in (("baseline", baseline), ("candidate", candidate)):
        margin_rows = [(g, {**p, "home_win_prob": p["margin_home_win_prob"]}) for g, p in rows]
        out[side]["win_margin_distribution"] = score_rows(margin_rows)["win_logistic"]
    for t in TARGETS:
        delta, dd = [], []
        for p in adjusted:
            actual = actuals(p["result"]).get(t)
            if actual is not None:
                delta.append(abs(actual-points(p["candidate"])[t])-abs(actual-points(p["baseline"])[t]))
                dd.append(parse_utc(p["result"]["date_utc"]).astimezone(ET).date().isoformat())
        out["paired_mae_delta"][t] = {"n": len(delta), "candidate_minus_baseline": sum(delta)/len(delta) if delta else None,
                                     "et_day_bootstrap_95": _day_bootstrap_ci(delta, dd)}
    out["paired_margin_win_delta"] = {}
    for metric in ("log_loss", "brier"):
        delta, dd = [], []
        for p in adjusted:
            y = float(p["result"]["home_pts"] > p["result"]["away_pts"])
            def loss(prob):
                prob = max(1e-12, min(1-1e-12, prob))
                return -(y*math.log(prob)+(1-y)*math.log(1-prob)) if metric == "log_loss" else (prob-y)**2
            delta.append(loss(p["candidate"]["margin_home_win_prob"])-loss(p["baseline"]["margin_home_win_prob"]))
            dd.append(parse_utc(p["result"]["date_utc"]).astimezone(ET).date().isoformat())
        out["paired_margin_win_delta"][metric] = {"n": len(delta),
            "candidate_minus_baseline": sum(delta)/len(delta) if delta else None, "et_day_bootstrap_95": _day_bootstrap_ci(delta, dd)}
    cur.execute("SELECT payload,effective_at FROM model_shadow_states WHERE experiment_version=%s ORDER BY effective_at DESC LIMIT 1", (VERSION,))
    state = cur.fetchone()
    out["state"] = {**json.loads(state["payload"]), "effective_at": state["effective_at"]} if state else None
    if out["state"]:
        out["state"].pop("training_result_ids", None)
    return out


def scheduled():
    if os.getenv("MODEL_SHADOW_ENABLED", "").lower() not in ("1", "true"):
        return {"status": "DISABLED"}
    result = collect(activate=True)
    if result["status"] == "COLLECTING":
        from ..db import cursor, heartbeat
        with cursor() as cur:
            heartbeat(cur, source_key="model_shadow", display_name="獨立影子校正實驗", category="model",
                      expected_interval_min=1, status="ok", records_updated=result["captured"])
    return result
