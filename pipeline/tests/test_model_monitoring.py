from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from core.production.monitoring import evaluate_snapshot, select_predictions

TIP = datetime(2026, 10, 21, 0, tzinfo=timezone.utc)


def snapshot():
    g = {"game_id": 1, "date_utc": TIP.isoformat(), "status": "final", "season_stage": "regular",
         "home_pts": 110, "away_pts": 100, "home_h1": 50, "away_h1": 45}
    p = {"id": 1, "game_id": 1, "model_version": "ml-v2.0",
         "created_at": (TIP - timedelta(minutes=65)).isoformat(), "home_win_prob": .75,
         "pred_margin": 8, "pred_total": 212, "pred_home_h1": 51, "pred_away_h1": 45,
         "features_json": {"model_version": "ml-v2.0", "artifact_version": "fixture-a",
                           "prediction_as_of_utc": (TIP - timedelta(minutes=66)).isoformat(),
                           "training_cutoff_utc": (TIP - timedelta(days=10)).isoformat(),
                           "profile": "final", "prediction_kind": "final"}}
    return {"checked_at": (TIP + timedelta(days=1)).isoformat(), "games": [g], "predictions": [p]}


def select(s, mode="final_t60"):
    return select_predictions(s["games"], s["predictions"], datetime.fromisoformat(s["checked_at"]), mode)[0]


def test_one_prediction_per_game_and_decision_time():
    s = snapshot()
    later = deepcopy(s["predictions"][0])
    later.update(id=2, created_at=(TIP - timedelta(minutes=20)).isoformat(), home_win_prob=.9)
    later["features_json"]["prediction_as_of_utc"] = later["created_at"]
    s["predictions"].extend([later, deepcopy(later)])
    assert select(s)[0][1]["id"] == 1
    assert select(s, "latest")[0][1]["id"] == 2
    report = evaluate_snapshot(s)["evaluations"]["final_t60"]
    assert report["n_games"] == 1
    assert report["win_logistic"]["brier"] == pytest.approx(.0625)


@pytest.mark.parametrize("mutation", ["backfilled", "future_training", "future_inference", "missing_artifact", "missing_cutoff", "wrong_model", "bad_timestamp"])
def test_invalid_provenance_is_not_counted(mutation):
    s = snapshot()
    p = s["predictions"][0]
    f = p["features_json"]
    if mutation == "backfilled":
        p["created_at"] = (TIP + timedelta(hours=3)).isoformat()
    elif mutation == "future_training":
        f["training_cutoff_utc"] = (TIP - timedelta(minutes=5)).isoformat()
    elif mutation == "future_inference":
        f["prediction_as_of_utc"] = (TIP + timedelta(hours=3)).isoformat()
    elif mutation == "missing_artifact":
        del f["artifact_version"]
    elif mutation == "missing_cutoff":
        del f["training_cutoff_utc"]
    elif mutation == "wrong_model":
        p["model_version"] = "ml-v1.0"
    else:
        f["prediction_as_of_utc"] = "invalid"
    assert not select(s, "latest")


def test_early_profile_in_refresh_is_available_early_evidence():
    s = snapshot()
    p = s["predictions"][0]
    p["features_json"].update(profile="early", prediction_kind="refresh")
    assert len(select(s, "early")) == 1
    assert not select(s)


def test_missing_half_scores_do_not_turn_into_zero_or_drop_full_game():
    s = snapshot()
    s["games"][0]["home_h1"] = None
    report = evaluate_snapshot(s)["evaluations"]["final_t60"]
    assert report["total"]["n"] == 1
    assert report["total"]["mae"] == 2
    assert report["total"]["bias_actual_minus_prediction"] == -2
    assert report["h1_total"] == {"n": 0}
    assert report["total"]["interval_coverage"]["80"] == {"n": 0, "coverage": None}


def test_interval_coverage_uses_stored_intervals():
    s = snapshot()
    s["predictions"][0]["features_json"]["predictive_distributions"] = {
        "total": {"interval_80": [210, 230], "interval_95": [200, 240]}}
    report = evaluate_snapshot(s)["evaluations"]["final_t60"]
    assert report["total"]["interval_coverage"]["80"] == {"n": 1, "coverage": 1.0}


def test_empty_data_and_missing_predictions_are_explicit():
    s = snapshot()
    s["predictions"] = []
    r = evaluate_snapshot(s)
    final = r["evaluations"]["final_t60"]
    assert final["coverage"] == 0
    assert final["missing_game_ids"] == [1]
    assert final["win_logistic"] == {"n": 0}
    assert "roi" not in r
    assert r["adaptation"]["production_changed"] is False
    s["games"] = []
    assert evaluate_snapshot(s)["adaptation"]["status"] == "WAITING"


def test_stage_and_unfinished_games_are_not_scored():
    for key, value in (("season_stage", "preseason"), ("status", "scheduled"), ("home_pts", None)):
        s = snapshot()
        s["games"][0][key] = value
        assert evaluate_snapshot(s)["n_final_games"] == 0


def test_bootstrap_and_snapshot_are_deterministic():
    s = snapshot()
    g, p = deepcopy(s["games"][0]), deepcopy(s["predictions"][0])
    g.update(game_id=2, date_utc=(TIP - timedelta(days=1)).isoformat(), home_pts=90)
    p.update(id=2, game_id=2, created_at=(TIP - timedelta(days=1, minutes=65)).isoformat())
    p["features_json"]["prediction_as_of_utc"] = p["created_at"]
    s["games"].append(g)
    s["predictions"].append(p)
    before = deepcopy(s)
    a = evaluate_snapshot(s)
    assert a == evaluate_snapshot(s)
    assert s == before
    ci = a["evaluations"]["final_t60"]["margin"]["bias_et_day_bootstrap_95"]
    assert ci[0] < 0 < ci[1]
