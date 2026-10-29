"""Offline process-boundary regressions: no configured DB or external API is used."""
from datetime import datetime
from contextlib import contextmanager
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core import job_process as worker
from core.timeutil import UTC


def test_injury_cadence_gate_still_runs_before_starting_worker(monkeypatch):
    from core.jobs import daily

    calls = []
    monkeypatch.setattr(worker, "run_isolated", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(daily, "injury_poll_job", lambda **k: None)
    worker.injury_poll_job()
    assert calls == []
    now = datetime(2026, 10, 30, 2, 15, tzinfo=UTC)
    monkeypatch.setattr(daily, "injury_poll_job", lambda *, fetch_job: fetch_job(now, expected_interval_min=60))
    worker.injury_poll_job()
    assert calls == [(("injuries",), {"now": now.isoformat(), "expected_interval_min": 60})]


def test_fixed_worker_command_uses_same_interpreter_and_inherits_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(worker, "resident_mib", lambda: 100.0)
    worker.run_isolated("retrain")
    (args, kwargs), = calls
    assert args == ([sys.executable, "-u", "-m", "core.job_process", "retrain"],)
    assert kwargs["input"] == "{}" and kwargs["check"] is True
    assert kwargs["cwd"] == worker.PIPELINE_DIR and kwargs["timeout"] == 3600
    assert "env" not in kwargs and "capture_output" not in kwargs and "stdout" not in kwargs
    assert not kwargs.get("shell", False)
    with pytest.raises(KeyError):
        worker.run_isolated("unregistered-job")


@pytest.mark.parametrize("job,kwargs,interval", [("injuries", {"expected_interval_min": 15}, 15),
                                               ("retrain", {}, 7 * 24 * 60)])
def test_failed_worker_heartbeat_keeps_source_and_cadence(monkeypatch, job, kwargs, interval):
    from core import db

    sentinel, calls = object(), []
    @contextmanager
    def cursor():
        yield sentinel
    monkeypatch.setattr(db, "cursor", cursor)
    monkeypatch.setattr(db, "heartbeat", lambda cur, **kw: calls.append((cur, kw)))
    worker._record_failure(job, "worker_timeout", kwargs)
    (cur, fields), = calls
    assert cur is sentinel and fields["status"] == "error"
    assert fields["error"] == "worker_timeout" and fields["expected_interval_min"] == interval
    assert fields["source_key"] == worker.JOBS[job][3]["source_key"]


@pytest.mark.parametrize("job,payload,function", [
    ("injuries", {"now": "2026-10-30T02:15:00+00:00", "expected_interval_min": 15}, "fetch_injuries_job"),
    ("retrain", {}, "retrain_job"),
])
def test_child_dispatches_existing_job_without_changing_inputs(monkeypatch, job, payload, function):
    calls = []
    fake = SimpleNamespace(**{function: lambda **k: calls.append(k)})
    monkeypatch.setattr(worker.importlib, "import_module", lambda name: fake)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert worker.main([job]) == 0
    assert len(calls) == 1
    if job == "injuries":
        assert calls[0] == {"now": datetime(2026, 10, 30, 2, 15, tzinfo=UTC), "expected_interval_min": 15}
    else:
        assert calls[0] == {}


@pytest.mark.parametrize("job,payload", [("retrain", {"cutoff": "future"}),
                                         ("injuries", {"now": "bad", "expected_interval_min": 15}),
                                         ("injuries", {"now": "2026-10-30T02:15:00Z", "expected_interval_min": 1})])
def test_child_invalid_request_never_imports_job(monkeypatch, job, payload):
    def must_not_import(name):
        pytest.fail("invalid request reached a job")
    monkeypatch.setattr(worker.importlib, "import_module", must_not_import)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert worker.main([job]) == 1


def test_real_child_exits_and_releases_large_allocation(monkeypatch, tmp_path):
    result = tmp_path / "worker.json"
    code = """
import json, os, sys
from pathlib import Path
allocated = bytearray(64 * 1024**2)
Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), 'allocated': len(allocated)}))
print('worker-log-streamed', flush=True)
"""
    monkeypatch.setattr(worker, "_command", lambda job: [sys.executable, "-c", code, str(result)])
    worker.run_isolated("retrain")
    data = json.loads(result.read_text())
    assert data["allocated"] == 64 * 1024**2 and data["pid"] != os.getpid()
    with pytest.raises(ProcessLookupError):
        os.kill(data["pid"], 0)


@pytest.mark.parametrize("mode,reason", [("exit", "worker_failed"), ("timeout", "worker_timeout")])
def test_real_worker_failure_is_reaped_and_marks_error(monkeypatch, tmp_path, mode, reason):
    pid_file = tmp_path / "pid"
    code = """
import os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
if sys.argv[2] == 'exit':
    raise SystemExit(7)
time.sleep(30)
"""
    monkeypatch.setattr(worker, "_command", lambda job: [sys.executable, "-c", code, str(pid_file), mode])
    module, fn, _, meta = worker.JOBS["retrain"]
    monkeypatch.setitem(worker.JOBS, "retrain", (module, fn, 1, meta))
    recorded = []
    monkeypatch.setattr(worker, "_record_failure", lambda *a: recorded.append(a))
    with pytest.raises(RuntimeError, match=reason):
        worker.run_isolated("retrain")
    assert recorded == [("retrain", reason, {})]
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_registering_jobs_never_imports_jvm_or_training_module():
    # Fresh interpreter: pytest itself imports numerical modules for other tests.
    code = """
import json, sys
from core.scheduling import build_scheduler
s = build_scheduler(run_on_start=False)
assert len(s.get_jobs()) == 21
assert 'nbainjuries' not in sys.modules and 'jpype' not in sys.modules
assert 'core.production.retrain' not in sys.modules
"""
    env = {**os.environ, "DATABASE_URL": "postgresql://invalid:invalid@127.0.0.1:1/none", "PYTHON_DOTENV_DISABLED": "1"}
    subprocess.run([sys.executable, "-c", code], cwd=worker.PIPELINE_DIR, env=env, check=True, timeout=30)
