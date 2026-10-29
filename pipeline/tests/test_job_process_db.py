"""Real worker subprocesses against only the fixture's random schema; no NBA API."""
import json
import sys

from core import job_process as worker
from core.config import settings
from core.timeutil import parse_utc


def _isolate_child_db(db, monkeypatch):
    with db.cursor() as cur:
        cur.execute("SELECT current_schema() AS s")
        schema = cur.fetchone()["s"]
    assert schema.startswith("c5a_test_")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    monkeypatch.setenv("PG_POOL_MAX", "2")
    monkeypatch.setenv("MEMORY_TEST_SCHEMA", schema)
    return schema


CHILD_SCHEMA_GUARD = """
import os
from core import db
with db.cursor() as cur:
    cur.execute('SELECT current_schema() AS s')
    assert cur.fetchone()['s'] == os.environ['MEMORY_TEST_SCHEMA']
"""


def test_injury_worker_writes_same_snapshot_and_is_idempotent(db, seeded, monkeypatch):
    _isolate_child_db(db, monkeypatch)
    # Stub only HTTP/PDF acquisition inside a real interpreter. Dispatch, DB pool,
    # transactions, ET->UTC conversion, ingestion and heartbeats stay real.
    code = CHILD_SCHEMA_GUARD + """
from datetime import datetime
from core.sources import injuries
from core.job_process import main
injuries.find_latest_report = lambda *a, **k: datetime(2026, 1, 15, 17)
injuries.fetch_injury_report = lambda *a: [
    {'game_date': '01/15/2026', 'matchup': 'BOS@NYK', 'team': 'Boston Celtics',
     'player_name': 'Brown, Jaylen', 'status': 'Questionable', 'reason': 'Synthetic fixture'}]
raise SystemExit(main(['injuries']))
"""
    monkeypatch.setattr(worker, "_command", lambda job: [sys.executable, "-c", code])
    for _ in range(2):
        worker.run_isolated("injuries", now="2026-01-15T22:07:00Z", expected_interval_min=15)
    with db.cursor() as cur:
        cur.execute("SELECT report_time_utc,status FROM injuries")
        row, = cur.fetchall()
        assert row["report_time_utc"] == parse_utc("2026-01-15T22:00:00Z")
        assert row["status"] == "Questionable"
        cur.execute("SELECT count(*) AS n FROM injury_reports")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT last_status,expected_interval_min FROM data_sources WHERE source_key='nbainjuries'")
        assert cur.fetchone() == {"last_status": "ok", "expected_interval_min": 15}


def test_retrain_worker_keeps_no_new_data_gate_and_artifact(db, monkeypatch, tmp_path):
    _isolate_child_db(db, monkeypatch)
    root = tmp_path / "artifacts"
    version = root / "versions" / "synthetic"
    version.mkdir(parents=True)
    (root / "CURRENT.json").write_text(json.dumps({"artifact_version": "synthetic"}))
    (version / "manifest.json").write_text(json.dumps({"metadata": {"n_games_history": 0}}))
    monkeypatch.setenv("MODEL_ARTIFACT_DIR", str(root))
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    code = CHILD_SCHEMA_GUARD + """
from core.job_process import main
raise SystemExit(main(['retrain']))
"""
    monkeypatch.setattr(worker, "_command", lambda job: [sys.executable, "-c", code])
    worker.run_isolated("retrain")
    assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    with db.cursor() as cur:
        cur.execute("SELECT last_status FROM data_sources WHERE source_key='model_retrain'")
        assert cur.fetchone()["last_status"] == "ok"
