"""Run the JVM / training work in disposable interpreters, never in the scheduler.

Only the two fixed jobs below are accepted. Business logic, database credentials,
artifact paths and Python version are inherited; no shell, pickle or job output
is buffered by the parent. The injury cadence gate stays in the parent.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

log = logging.getLogger(__name__)
PIPELINE_DIR = Path(__file__).resolve().parents[1]
JOBS = {
    "injuries": ("core.jobs.daily", "fetch_injuries_job", 600,
                 {"source_key": "nbainjuries", "display_name": "NBA 官方傷病報告", "category": "injury"}),
    "retrain": ("core.production.retrain", "retrain_job", 3600,
                {"source_key": "model_retrain", "display_name": "模型重訓（ml-v2.0）", "category": "model"}),
}


def resident_mib() -> float | None:
    """Current RSS, not the all-time high-water mark. No additional dependency."""
    try:
        if sys.platform.startswith("linux"):
            pages = int(Path("/proc/self/statm").read_text().split()[1])
            return round(pages * os.sysconf("SC_PAGE_SIZE") / 1024**2, 1)
        if sys.platform == "darwin":
            rss = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True)
            return round(int(rss.strip()) / 1024, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _command(job: str) -> list[str]:
    return [sys.executable, "-u", "-m", "core.job_process", job]


def _record_failure(job: str, reason: str, kwargs: dict) -> None:
    from .db import cursor, heartbeat

    interval = kwargs.get("expected_interval_min", 30) if job == "injuries" else 7 * 24 * 60
    with cursor() as cur:
        heartbeat(cur, status="error", error=reason, expected_interval_min=interval, **JOBS[job][3])


def run_isolated(job: str, **kwargs) -> None:
    spec = JOBS[job]  # reject unknown jobs before launching a process
    started, before = time.monotonic(), resident_mib()
    log.info("[memory-worker] %s starting parent_rss_mib=%s", job, before)
    try:
        # Inherit stdout/stderr: PDF / training logs cannot build up in parent RAM.
        subprocess.run(_command(job), input=json.dumps(kwargs), text=True, check=True,
                       cwd=PIPELINE_DIR, timeout=spec[2])
    except (OSError, subprocess.SubprocessError) as exc:
        reason = "worker_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "worker_failed"
        try:
            _record_failure(job, reason, kwargs)
        except Exception:  # a failed DB must not turn a failed worker into a success
            log.error("[memory-worker] %s could not record failure heartbeat", job)
        raise RuntimeError(f"{job}: {reason}") from None
    finally:
        log.info("[memory-worker] %s finished elapsed_s=%.1f parent_rss_mib=%s",
                 job, time.monotonic() - started, resident_mib())


def _fetch_injuries(now, *, expected_interval_min):
    return run_isolated("injuries", now=now.isoformat(), expected_interval_min=expected_interval_min)


def injury_poll_job() -> None:
    from .jobs.daily import injury_poll_job as poll

    # Keeps the existing next-game / cadence check and idle observability in parent.
    return poll(fetch_job=_fetch_injuries)


def retrain_job() -> None:
    return run_isolated("retrain")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=JOBS)
    args = parser.parse_args(argv)
    from .logging_conf import setup_logging

    setup_logging()
    try:
        payload = json.load(sys.stdin)
        if args.job == "injuries":
            from .timeutil import parse_utc

            if set(payload) != {"now", "expected_interval_min"}:
                raise ValueError("invalid injury worker arguments")
            payload["now"] = parse_utc(payload["now"])
            if payload["expected_interval_min"] not in (15, 30, 60, 120):
                raise ValueError("invalid injury interval")
        elif payload != {}:
            raise ValueError("retrain worker takes no arguments")
        module, function, _, _ = JOBS[args.job]
        getattr(importlib.import_module(module), function)(**payload)
        return 0
    except Exception as exc:
        log.error("[memory-worker] %s failed (%s)", args.job, type(exc).__name__)
        return 1
    finally:
        # Close the worker's own pool, never share scheduler connections across PIDs.
        db_module = sys.modules.get("core.db")
        pool = getattr(db_module, "_pool", None)
        if pool is not None:
            pool.close()
        scale = 1024**2 if sys.platform == "darwin" else 1024
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale
        log.info("[memory-worker] %s exiting peak_rss_mib=%.1f", args.job, peak)


if __name__ == "__main__":
    raise SystemExit(main())
