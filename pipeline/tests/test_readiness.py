"""Production readiness 分類（純函式）+ docker-entrypoint.sh 的 bootstrap / 保留行為 + 部署檔案不變式。"""
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.production import readiness as rd

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
PIPELINE = Path(__file__).resolve().parents[1]
REPO = PIPELINE.parent


def hb(minutes_ago, status="ok", **kw):
    return {"last_attempt_at": NOW - timedelta(minutes=minutes_ago), "last_success_at": NOW - timedelta(minutes=minutes_ago),
            "last_status": status, "last_error": kw.get("err"), "last_outcome": kw.get("outcome"), "meta": kw.get("meta")}


# ---- heartbeat ---------------------------------------------------------------------------------------------

def test_heartbeat_fresh_is_pass():
    assert rd.classify_heartbeat(hb(2), NOW, expected_min=5, critical=True, post_deploy=True)[0] == rd.PASS


def test_heartbeat_stale_before_deploy_is_waiting_after_deploy_fail_or_warn():
    stale = hb(600)
    assert rd.classify_heartbeat(stale, NOW, expected_min=5, critical=True, post_deploy=False)[0] == rd.WAITING
    assert rd.classify_heartbeat(stale, NOW, expected_min=5, critical=True, post_deploy=True)[0] == rd.FAIL
    assert rd.classify_heartbeat(stale, NOW, expected_min=5, critical=False, post_deploy=True)[0] == rd.WARN


def test_heartbeat_missing_and_error():
    assert rd.classify_heartbeat(None, NOW, expected_min=5, critical=True, post_deploy=False)[0] == rd.WAITING
    assert rd.classify_heartbeat(None, NOW, expected_min=5, critical=True, post_deploy=True)[0] == rd.FAIL
    assert rd.classify_heartbeat(hb(1, "error", err="boom"), NOW, expected_min=5, critical=False,
                                 post_deploy=False)[0] == rd.WARN


# ---- 預期中的等待 / 阻擋不是錯誤 -------------------------------------------------------------------------------

def test_zero_future_games_is_waiting_not_error():
    assert rd.classify_future_games(0, 0)[0] == rd.WAITING
    assert rd.classify_future_games(0, 40)[0] == rd.WAITING
    assert rd.classify_future_games(3, 40)[0] == rd.PASS


def test_predictions_waiting_vs_warn():
    assert rd.classify_predictions(0, 0, 0)[0] == rd.WAITING
    assert rd.classify_predictions(10, 0, 0)[0] == rd.PASS
    assert rd.classify_predictions(10, 5, 0)[0] == rd.PASS
    assert rd.classify_predictions(10, 5, 2)[0] == rd.WARN


def test_twsport_disabled_or_blocked_is_blocked_not_fail():
    assert rd.classify_twsport(None, None, NOW, enabled=False, games_in_window=3)[0] == rd.BLOCKED
    assert rd.classify_twsport(hb(5, outcome="blocked"), None, NOW, enabled=True, games_in_window=3)[0] == rd.BLOCKED
    assert rd.classify_twsport(hb(5, outcome="success"), NOW, NOW, enabled=True, games_in_window=3)[0] == rd.PASS
    assert rd.classify_twsport(hb(5, outcome="no_nba_markets"), None, NOW, enabled=True, games_in_window=0)[0] == rd.WAITING
    assert rd.classify_twsport(hb(5, outcome=None), None, NOW, enabled=True, games_in_window=0)[0] == rd.WAITING


def test_oddsapi_states():
    c = dict(key_set=True, warn_at=100, reserve=25, post_deploy=True)
    assert rd.classify_oddsapi(hb(60, outcome="no_nba_markets"), None, NOW, **c)[0] == rd.WAITING
    assert rd.classify_oddsapi(hb(60, outcome="success", meta={"quota_remaining": 400}), None, NOW, **c)[0] == rd.PASS
    assert rd.classify_oddsapi(hb(60, outcome="success", meta={"quota_remaining": 80}), None, NOW, **c)[0] == rd.WARN
    assert rd.classify_oddsapi(hb(60, outcome="quota_low", meta={"quota_remaining": 10}), None, NOW, **c)[0] == rd.WARN
    assert rd.classify_oddsapi(hb(60, outcome="auth_failed"), None, NOW, **c)[0] == rd.FAIL
    assert rd.classify_oddsapi(None, None, NOW, **c)[0] == rd.WAITING
    assert rd.classify_oddsapi(None, None, NOW, **{**c, "key_set": False})[0] == rd.WARN


def test_database_url_classification_never_echoes_url():
    assert rd.classify_database_url(None)[0] == rd.FAIL
    st, d = rd.classify_database_url("postgresql://postgres.abc:SECRETPW@aws-1.pooler.supabase.com:5432/postgres")
    assert st == rd.PASS and "SECRETPW" not in d and "abc" not in d
    assert rd.classify_database_url("postgresql://u:p@aws-1.pooler.supabase.com:6543/postgres")[0] == rd.WARN
    assert rd.classify_database_url("postgresql://u:p@db.abcd.supabase.co:5432/postgres")[0] == rd.WARN
    assert rd.classify_database_url("mysql://u:p@h:3306/x")[0] == rd.FAIL


def test_environment_check_reports_names_only():
    env = {"DATABASE_URL": "postgresql://u:TOPSECRET@x.pooler.supabase.com:5432/p", "ODDS_API_KEY": "KEYVALUE123",
           "TWSPORT_ENABLED": "false", "MODEL_ARTIFACT_DIR": "/data/model-artifacts", "TZ": "Asia/Taipei"}
    checks = rd.check_environment(env)
    blob = " ".join(c.detail for c in checks)
    assert "TOPSECRET" not in blob and "KEYVALUE123" not in blob
    assert all(c.status == rd.PASS for c in checks)


def test_worst_ordering():
    cs = [rd.Check("a", "b", s, "") for s in (rd.PASS, rd.WAITING, rd.BLOCKED)]
    assert rd.worst(cs) == rd.BLOCKED
    assert rd.worst(cs + [rd.Check("a", "b", rd.FAIL, "")]) == rd.FAIL
    assert rd.worst([rd.Check("a", "b", rd.WAITING, ""), rd.Check("a", "b", rd.WARN, "")]) == rd.WARN


def test_artifact_check_fails_when_missing(tmp_path):
    out = rd.check_artifact(tmp_path)
    assert out[0].status == rd.FAIL


def test_artifact_check_passes_for_committed_bootstrap():
    out = rd.check_artifact(PIPELINE / "artifacts" / "production")
    assert out[0].status == rd.PASS, out[0].detail


def test_scheduler_definition_check_passes():
    out = rd.check_scheduler_definition(NOW)
    assert out[0].status == rd.PASS


# ---- docker-entrypoint.sh ---------------------------------------------------------------------------------

def _run_entrypoint(tmp_path, volume: Path):
    bootstrap = tmp_path / "bootstrap"
    if not bootstrap.exists():
        (bootstrap / "versions" / "v1").mkdir(parents=True)
        (bootstrap / "CURRENT.json").write_text('{"artifact_version": "v1"}')
        (bootstrap / "versions" / "v1" / "bundle.joblib").write_text("x")
        (bootstrap / ".lock").write_text("")
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    py = fake / "python"
    py.write_text("#!/bin/sh\necho started:$@\n")
    py.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "MODEL_ARTIFACT_DIR": str(volume),
           "BOOTSTRAP_ARTIFACT_DIR": str(bootstrap)}
    return subprocess.run(["sh", str(PIPELINE / "docker-entrypoint.sh")], env=env, capture_output=True, text=True, cwd=tmp_path)


def test_entrypoint_bootstraps_empty_volume_then_preserves_it(tmp_path):
    vol = tmp_path / "vol"
    r = _run_entrypoint(tmp_path, vol)
    assert r.returncode == 0, r.stderr
    assert "No persistent production artifact found; bootstrapping" in r.stdout
    assert "Bootstrap artifact copied." in r.stdout and "started:scheduler.py" in r.stdout
    assert (vol / "CURRENT.json").exists() and (vol / "versions" / "v1" / "bundle.joblib").exists()
    assert not (vol / ".lock").exists()
    # 之後重訓 promote 了新版本；重新部署不得覆蓋
    (vol / "CURRENT.json").write_text('{"artifact_version": "v2-retrained"}')
    r = _run_entrypoint(tmp_path, vol)
    assert "already exists; keeping it" in r.stdout
    assert "v2-retrained" in (vol / "CURRENT.json").read_text()


def test_entrypoint_is_executable_and_lf():
    p = PIPELINE / "docker-entrypoint.sh"
    assert os.access(p, os.X_OK)
    assert b"\r" not in p.read_bytes()


# ---- 部署檔案不變式 ----------------------------------------------------------------------------------------

def test_bootstrap_artifact_tracked_but_lock_and_training_files_are_not():
    ls = subprocess.run(["git", "ls-files", "pipeline/artifacts"], cwd=REPO, capture_output=True, text=True).stdout.split()
    assert "pipeline/artifacts/production/CURRENT.json" in ls
    assert not any(f.endswith(".lock") for f in ls)
    assert not any(f.endswith((".pkl", ".csv.gz")) for f in ls)


def test_dockerignore_keeps_production_artifact_and_drops_har():
    text = (PIPELINE / ".dockerignore").read_text()
    assert "artifacts/*" in text and "!artifacts/production" in text and "*.har" in text


def test_har_files_are_gitignored():
    r = subprocess.run(["git", "check-ignore", "-q", "pipeline/captures/x.har"], cwd=REPO)
    assert r.returncode == 0


# ---- HAR helper ------------------------------------------------------------------------------------------

def test_har_helper_counts_sensitive_without_printing_and_shreds(tmp_path):
    import run_twsport_har as h
    har = {"log": {"entries": [{"request": {"headers": [{"name": "Cookie", "value": "SECRET"}]}, "response": {}},
                               {"request": {"headers": [{"name": "Accept", "value": "x"}]},
                                "response": {"headers": [{"name": "Set-Cookie", "value": "SECRET2"}]}},
                               {"request": {"headers": []}, "response": {}}]}}
    assert h.sensitive_entry_count(har) == 2
    p = tmp_path / "c.har"
    p.write_text("x" * 100)
    h.shred(p)
    assert not p.exists()
