from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.models.distributions import FittedDistribution
from core.production import shadow, artifact, observability

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def fake_artifact():
    distributions = {t: {"state": FittedDistribution(t, "gaussian", 0, 14).to_state(),
                         "distribution_version": "dist-v2", "spec": {"location": "zero"},
                         "fit_set_summary": {"coverage": {}}} for t in shadow.TARGETS}
    return SimpleNamespace(artifact_version="fixture-artifact", model_version="ml-v2.0",
                           profile=lambda _: {"distributions": distributions})


def baseline(tip=NOW):
    return {"id": 1, "game_id": 1, "model_version": "ml-v2.0", "home_win_prob": .6,
            "created_at": (tip-timedelta(minutes=70)).isoformat(),
            "pred_margin": 4., "pred_total": 220., "pred_home_h1": 57., "pred_away_h1": 53.,
            "features_json": {"model_version": "ml-v2.0", "artifact_version": "fixture-artifact", "profile": "final",
                "prediction_as_of_utc": (tip-timedelta(minutes=71)).isoformat(),
                "training_cutoff_utc": (tip-timedelta(days=30)).isoformat(),
                "distribution_context": {"min_gp": 10, "inj_both_known": 1, "is_playoffs": 0}}}


def training_pairs(n=35):
    out = []
    for i in range(n):
        tip = NOW-timedelta(days=10+i//5)
        out.append({"prediction": baseline(tip), "result_id": str(i), "observed_at": (tip+timedelta(hours=4)).isoformat(),
            "result": {"date_utc": tip.isoformat(), "status": "final", "home_pts": 120, "away_pts": 110,
                       "home_h1": 60, "away_h1": 55}})
    return out


def test_shrinkage_warmup_cap_and_missing_halves():
    pairs = training_pairs()
    fitted = shadow.fit_state(pairs, NOW)
    assert fitted["shifts"]["margin"] == pytest.approx(35*6/235)
    assert fitted["shifts"]["total"] == pytest.approx(35*10/235)
    assert shadow.fit_state(pairs[:29], NOW)["shifts"]["total"] == 0
    few_days = deepcopy(pairs)
    for p in few_days:
        p["result"]["date_utc"] = (NOW-timedelta(days=10)).isoformat()
    assert shadow.fit_state(few_days, NOW)["shifts"]["total"] == 0
    for p in pairs:
        p["result"].update(home_h1=None, away_h1=None, home_pts=500)
    fitted = shadow.fit_state(pairs, NOW)
    assert fitted["shifts"]["total"] == 2
    assert fitted["shifts"]["h1_total"] == 0
    assert fitted["n_by_target"]["h1_total"] == 0


def test_future_results_cannot_change_past_state():
    pairs = training_pairs()
    expected = shadow.fit_state(pairs, NOW)
    future = deepcopy(pairs)
    for p in future:
        p["observed_at"] = (NOW+timedelta(seconds=1)).isoformat()
        p["result"]["home_pts"] = 999
    assert shadow.fit_state(pairs+future, NOW) == expected
    boundary = deepcopy(pairs)
    for p in boundary:
        p["observed_at"] = NOW.isoformat()
    assert shadow.fit_state(boundary, NOW)["n_by_target"]["margin"] == 0


def test_shift_uses_same_distribution_and_does_not_mutate_logistic_or_input():
    p = baseline()
    before = deepcopy(p)
    shifts = dict(margin=-1, total=2, h1_margin=-2, h1_total=1)
    candidate = shadow.shifted_prediction(p, shifts, fake_artifact())
    assert shadow.points(candidate) == dict(margin=3., total=222., h1_margin=2., h1_total=111.)
    assert candidate["home_win_prob"] == p["home_win_prob"]
    assert p == before
    assert 0 < candidate["margin_home_win_prob"] < 1
    zero = shadow.shifted_prediction(p, dict.fromkeys(shadow.TARGETS, 0.), fake_artifact())
    assert shadow.points(zero) == shadow.points(p)
    assert zero["features_json"]["predictive_distributions"]["total"]["scale"] == 14
    with pytest.raises(ValueError):
        shadow.shifted_prediction(p, dict.fromkeys(shadow.TARGETS, 3.), fake_artifact())


def test_disabled_job_does_not_open_database(monkeypatch):
    monkeypatch.delenv("MODEL_SHADOW_ENABLED", raising=False)
    monkeypatch.setattr(shadow, "collect", lambda **_: pytest.fail("must not collect"))
    assert shadow.scheduled()["status"] == "DISABLED"


def test_unchanged_results_need_bounded_database_reads_as_ledger_grows():
    tip = NOW-timedelta(days=1)
    game = dict(game_id=1,date_utc=tip,status='final',home_pts=110,away_pts=100,home_h1=55,away_h1=50)
    rows = [{**game,'id':str(i),'scheduled_tipoff':tip,'previous_payload':shadow.dumps(game)} for i in range(1000)]
    class Cursor:
        calls = 0
        def execute(self, sql, args=None):
            self.calls += 1
            assert self.calls <= 2, 'unchanged results must not require per-game database round trips'
        def fetchone(self):
            return {'now':NOW}
        def fetchall(self):
            return rows
    cur = Cursor()
    assert shadow._observe_results(cur) == 0
    assert cur.calls == 2


def seed_prediction(db, teams, tip):
    with db.cursor() as cur:
        cur.execute("""INSERT INTO games (nba_game_id,season,season_stage,date_utc,status,home_team_id,away_team_id)
            VALUES (%s,'2026-27','regular',%s,'scheduled',%s,%s) RETURNING id""", (str(uuid4()), tip, teams["BOS"], teams["NYK"]))
        gid = cur.fetchone()["id"]
        p = baseline(tip)
        cur.execute("""INSERT INTO predictions (game_id,model_version,created_at,home_win_prob,pred_margin,pred_total,
            pred_home_h1,pred_away_h1,features_json) VALUES (%s,'ml-v2.0',%s,.6,4,220,57,53,%s::jsonb) RETURNING id""",
            (gid, p["created_at"], shadow.dumps(p["features_json"])))
        p.update(id=cur.fetchone()["id"], game_id=gid)
    return gid, p


def prepare(db, monkeypatch):
    monkeypatch.setattr(artifact, "load_version", lambda *a: fake_artifact())
    with db.cursor() as cur:
        cur.execute("SELECT current_schema() AS s")
        schema = cur.fetchone()["s"]
        assert schema.startswith("c5a_test_")
    monkeypatch.setattr(shadow, "LOCK_KEY", f"shadow-test:{schema}")


def test_db_capture_is_idempotent_and_leaves_production_predictions_unchanged(db, seeded, monkeypatch):
    prepare(db, monkeypatch)
    with db.cursor() as cur:
        now = shadow._clock(cur)
    gid, p = seed_prediction(db, seeded, now+timedelta(minutes=70))
    # Fixture prediction created before the server's current time.
    first = shadow.collect(activate=True)
    assert first["captured"] == 1
    assert shadow.collect()["captured"] == 0
    with db.cursor() as cur:
        cur.execute("SELECT * FROM model_shadow_predictions")
        saved = cur.fetchone()
        body = json.loads(saved["payload"])
        assert body["candidate"]["pred_total"] == body["baseline"]["pred_total"]
        assert saved["captured_at"] <= saved["scheduled_tipoff"]-timedelta(minutes=60)
        cur.execute("SELECT count(*) AS n FROM predictions")
        assert cur.fetchone()["n"] == 1
        with pytest.raises(Exception, match="append-only"):
            cur.execute("UPDATE model_shadow_predictions SET payload='{}'")
        cur.execute("UPDATE games SET status='final',home_pts=120,away_pts=100 WHERE id=%s", (gid,))
    # A future game erroneously marked final is not an available result.
    assert shadow.collect()["results_observed"] == 0


def test_db_missed_window_is_never_backfilled(db, seeded, monkeypatch):
    prepare(db, monkeypatch)
    with db.cursor() as cur:
        now = shadow._clock(cur)
    seed_prediction(db, seeded, now+timedelta(minutes=59))
    assert shadow.collect(activate=True)["captured"] == 0


def test_db_concurrent_collectors_cannot_duplicate_pairs(db, seeded, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    prepare(db, monkeypatch)
    with db.cursor() as cur:
        now = shadow._clock(cur)
    seed_prediction(db, seeded, now+timedelta(minutes=70))
    barrier = Barrier(2)
    def run():
        barrier.wait()
        return shadow.collect(activate=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert sum(r.get('captured', 0) for r in results) == 1
    with db.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM model_shadow_predictions')
        assert cur.fetchone()['n'] == 1


def seed_past_pair(db, teams):
    with db.cursor() as cur:
        now = shadow._clock(cur)
    tip = now-timedelta(days=2)
    gid, p = seed_prediction(db, teams, tip)
    exp_start = tip-timedelta(days=1)
    p = shadow.shifted_prediction(p, dict.fromkeys(shadow.TARGETS, 0.), fake_artifact())
    with db.cursor() as cur:
        cur.execute("INSERT INTO model_shadow_experiments VALUES (%s,%s,%s)", (shadow.VERSION, exp_start, shadow.dumps(shadow.SPEC)))
        cur.execute("INSERT INTO model_shadow_states VALUES ('state',%s,'initial',%s,%s)",
                    (shadow.VERSION, exp_start, shadow.dumps(shadow.fit_state([], exp_start))))
        cur.execute("INSERT INTO model_shadow_predictions VALUES ('pair',%s,%s,%s,'state',%s,%s,%s)",
                    (shadow.VERSION, gid, p["id"], tip, tip-timedelta(minutes=65),
                     shadow.dumps({"baseline": p, "candidate": p, "shifts": dict.fromkeys(shadow.TARGETS, 0.)})))
        cur.execute("UPDATE games SET status='final',home_pts=110,away_pts=100,home_h1=55,away_h1=50 WHERE id=%s", (gid,))
    return gid


def test_db_result_corrections_append_and_asof_training_uses_observed_version(db, seeded, monkeypatch):
    prepare(db, monkeypatch)
    gid = seed_past_pair(db, seeded)
    assert shadow.collect()["results_observed"] == 1
    with db.cursor() as cur:
        cutoff = shadow._clock(cur)
        assert shadow.summary(cur, cutoff)["n_pairs"] == 1
        cur.execute("UPDATE games SET home_pts=112 WHERE id=%s", (gid,))
    assert shadow.collect()["results_observed"] == 1
    assert shadow.collect()["results_observed"] == 0
    with db.cursor() as cur:
        assert shadow._pairs(cur, cutoff)[0]["result"]["home_pts"] == 110
        assert shadow._pairs(cur, shadow._clock(cur))[0]["result"]["home_pts"] == 112
        cur.execute("SELECT count(*) AS n FROM model_shadow_results")
        assert cur.fetchone()["n"] == 2
        cur.execute("UPDATE games SET date_utc=date_utc+interval '1 hour' WHERE id=%s", (gid,))
    assert shadow.collect()["results_observed"] == 1
    with db.cursor() as cur:
        assert shadow._pairs(cur, shadow._clock(cur)) == []


def test_db_frozen_spec_and_week_state_are_preserved(db, seeded, monkeypatch):
    prepare(db, monkeypatch)
    seed_past_pair(db, seeded)
    with db.transaction() as cur:
        exp = shadow._experiment(cur)
        monday = datetime(2026, 11, 16, 16, 11, tzinfo=shadow.TPE)
        monkeypatch.setattr(shadow, "_clock", lambda _: monday)
        # Use an earlier activation only for this isolated test object.
        exp["activated_at"] = monday-timedelta(days=14)
        first = shadow._state(cur, exp)
        monkeypatch.setattr(shadow, "_clock", lambda _: monday+timedelta(days=7, minutes=-2))
        assert shadow._state(cur, exp)["id"] == first["id"]
    monkeypatch.setitem(shadow.SPEC, "max_shift_points", 1.9)
    with db.cursor() as cur, pytest.raises(ValueError, match="new experiment version"):
        shadow._experiment(cur)


def test_db_materialization_writes_only_aggregates(db, monkeypatch):
    prepare(db, monkeypatch)
    monkeypatch.setattr(observability, "fetch_snapshot", lambda _: {
        "checked_at": NOW.isoformat(), "games": [], "predictions": []})
    assert observability.materialize()["n_final_games"] == 0
    with db.cursor() as cur:
        cur.execute("SELECT report_key,payload FROM model_evaluation_reports ORDER BY report_key")
        rows = cur.fetchall()
        assert len(rows) == 2
        assert json.loads(rows[0]["payload"])["status"] == "NOT_ACTIVATED"
        assert "user_id" not in shadow.dumps(rows)
