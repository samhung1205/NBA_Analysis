"""手動盤口離線檢查：不能接 DB，不能把 T-60 後擷取或過早盤口說成可用。"""
import builtins
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import run_twsport_har as helper

FX = Path(__file__).parent / "fixtures" / "odds"
NOW = datetime(2026, 10, 4, 4, 50, tzinfo=timezone.utc)


def raw():
    return json.loads((FX / "twsport_raw_preseason_20261004.json").read_text())


def test_inspection_market_coverage_and_original_capture_time():
    from core.odds import twsport
    tip = twsport.parse_raw(raw()).events[0].commence_utc
    captured = tip - timedelta(minutes=90)
    result = helper.inspect_capture(raw(), captured, now=captured)
    assert result["mode"] == "offline_parse_only" and result["database_accessed"] is False
    assert result["fetched_at"] == captured.isoformat()
    ev = result["events"][0]
    assert ev["capture_timing"] == "timing_compatible"
    assert ev["markets"] == ["ml", "spread", "total"]
    assert ev["missing_markets"] == ["h1_ml", "h1_spread", "h1_total"]
    assert ev["stage"] == "preseason"  # 解析成功不冒充正式決策資格


@pytest.mark.parametrize("offset, expected", [(59, "captured_after_cutoff"), (60, "timing_compatible"),
                                                 (120, "timing_compatible"), (121, "capture_too_early")])
def test_capture_window_boundaries(offset, expected):
    from core.odds import twsport
    tip = twsport.parse_raw(raw()).events[0].commence_utc
    captured = tip - timedelta(minutes=offset)
    result = helper.inspect_capture(raw(), captured, now=captured + timedelta(minutes=180))
    assert result["events"][0]["capture_timing"] == expected
    assert result["older_than_import_limit"] is True
    assert result["events"][0]["decision_time_passed"] is True


def test_inspect_cli_never_imports_config_db_or_ingest(tmp_path, monkeypatch, capsys):
    import sys
    from core import timeutil
    eg = raw()["tournaments"][2]["event_groups"][0]
    har = {"log": {"entries": [{"startedDateTime": "2026-10-04T04:40:00Z", "time": 2500,
            "request": {"url": "https://www-talo-ssb-pr.sportslottery.com.tw/services/content/get",
                        "headers": [{"name": "Cookie", "value": "SECRET_COOKIE"}],
                        "postData": {"text": json.dumps({"contentId": {"type": "eventGroup", "id": "63733.1"}})}},
            "response": {"status": 200, "content": {"text": json.dumps(eg)}}}]}}
    path = tmp_path / "capture.har"
    path.write_text(json.dumps(har))
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name not in ("core.config", "core.db", "core.odds.ingest"), f"forbidden import {name}"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(sys, "argv", ["run_twsport_har.py", str(path), "--inspect"])
    monkeypatch.setattr(timeutil, "now_utc", lambda: NOW)
    assert helper.main() == 0
    output = capsys.readouterr().out
    assert '"database_accessed": false' in output and "SECRET_COOKIE" not in output
    assert "2026-10-04T04:40:02.500000+00:00" in output
    assert path.exists()


@pytest.mark.parametrize("args", [["--inspect", "--import"], ["--inspect", "--delete-after"]])
def test_inspect_rejects_mutation_flags(args, monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", ["run_twsport_har.py", "absent.har", *args])
    with pytest.raises(SystemExit) as e:
        helper.main()
    assert e.value.code == 2
