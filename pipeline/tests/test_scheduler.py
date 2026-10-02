"""排程：job 不得是 paused、時區必須是台灣、next-run-time 可驗證"""
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.scheduling import job_specs, next_run_times, register_jobs
from core.timeutil import TPE, UTC


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def _started(run_on_start):
    sched = BackgroundScheduler(timezone=TPE)
    register_jobs(sched, run_on_start=run_on_start)
    sched.start(paused=True)      # 不真的觸發 job，但會為每個 job 算出 next_run_time
    return sched


@pytest.mark.parametrize("run_on_start", [False, True])
def test_no_job_is_paused(run_on_start):
    sched = _started(run_on_start)
    try:
        jobs = {j.id: j for j in sched.get_jobs()}
        assert set(jobs) == {s.id for s in job_specs()}
        for jid, job in jobs.items():
            assert job.next_run_time is not None, f"{jid} 被建立成 paused（next_run_time=None）"
    finally:
        sched.shutdown(wait=False)


def test_regression_next_run_time_none_means_paused():
    """記錄舊 bug：add_job(next_run_time=None) 在 APScheduler 3.x 是『暫停』而非『由 trigger 決定』。"""
    sched = BackgroundScheduler(timezone=TPE)
    sched.add_job(lambda: None, CronTrigger(hour=12, minute=0, timezone=TPE), id="old", next_run_time=None)
    sched.start(paused=True)
    try:
        assert sched.get_job("old").next_run_time is None
    finally:
        sched.shutdown(wait=False)


def test_startup_catchup_runs_once_then_follows_trigger():
    sched = _started(run_on_start=True)
    try:
        daily = sched.get_job("daily_schedule_scores")
        assert daily.next_run_time <= datetime.now(TPE) + timedelta(seconds=5)      # 補跑一次
        # 補跑之後的下一次仍由 trigger 決定：台灣 12:00
        after = daily.trigger.get_next_fire_time(daily.next_run_time, daily.next_run_time + timedelta(seconds=1))
        assert (after.hour, after.minute) == (12, 0) and after.utcoffset() == timedelta(hours=8)
        # injuries_offpeak 不補跑，避免與 peak 同時重複
        assert sched.get_job("injuries_offpeak").next_run_time > datetime.now(TPE) - timedelta(seconds=1)
    finally:
        sched.shutdown(wait=False)


def test_every_cron_trigger_is_explicitly_taipei():
    """CronTrigger 未指定 timezone 會用機器本地時區（容器多半 UTC → 台灣 20:00 才跑）。"""
    for s in job_specs():
        if isinstance(s.trigger, CronTrigger):
            assert str(s.trigger.timezone) == "Asia/Taipei", s.id


@pytest.mark.parametrize("now,expected", [
    (utc(2026, 10, 2, 3, 0), datetime(2026, 10, 2, 12, 0, tzinfo=TPE)),    # 台灣 11:00 → 今天 12:00
    (datetime(2026, 10, 2, 4, 0, 1, tzinfo=UTC), datetime(2026, 10, 3, 12, 0, tzinfo=TPE)),  # 台灣 12:00:01 → 明天（已錯過）
    (utc(2026, 10, 2, 5, 0), datetime(2026, 10, 3, 12, 0, tzinfo=TPE)),    # 台灣 13:00 → 明天 12:00
    (utc(2026, 10, 2, 15, 0), datetime(2026, 10, 3, 12, 0, tzinfo=TPE)),   # 台灣 23:00
])
def test_daily_schedule_next_run_is_noon_taipei(now, expected):
    nxt = next_run_times(job_specs(), now)["daily_schedule_scores"]
    assert nxt == expected
    assert nxt.astimezone(UTC).hour == 4          # 12:00 TPE = 04:00 UTC，與機器時區無關


def test_injury_polling_is_every_15_min_in_peak_and_30_min_off_peak():
    specs = job_specs()
    def soonest(now):
        t = next_run_times(specs, now)
        return min(t["injuries_peak"], t["injuries_offpeak"])
    # 台灣 10:40（尖峰尾端）→ 10:45
    assert soonest(utc(2026, 10, 2, 2, 40)) == datetime(2026, 10, 2, 10, 45, tzinfo=TPE)
    # 台灣 10:50 → 離峰 11:00（peak 已過 10:59）
    assert soonest(utc(2026, 10, 2, 2, 50)) == datetime(2026, 10, 2, 11, 0, tzinfo=TPE)
    # 台灣 11:05 → 離峰 11:30（不是 11:15）
    assert soonest(utc(2026, 10, 2, 3, 5)) == datetime(2026, 10, 2, 11, 30, tzinfo=TPE)
    # 台灣 23:45 → 隔日 00:00（尖峰起點）
    assert soonest(utc(2026, 10, 2, 15, 45)) == datetime(2026, 10, 3, 0, 0, tzinfo=TPE)
    # 台灣 05:07 → 05:15
    assert soonest(utc(2026, 10, 1, 21, 7)) == datetime(2026, 10, 2, 5, 15, tzinfo=TPE)


def test_live_refresh_is_a_5_minute_interval():
    trig = {s.id: s.trigger for s in job_specs()}["live_final_refresh"]
    assert trig.interval == timedelta(minutes=5)
    prev = datetime(2026, 10, 2, 11, 0, tzinfo=TPE)
    assert trig.get_next_fire_time(prev, prev + timedelta(seconds=1)) == prev + timedelta(minutes=5)


def test_guarded_swallows_exceptions():
    from core.scheduling import guarded

    def boom():
        raise RuntimeError("x")
    assert guarded(boom)() is None
