"""D.1 盤口排程：時間（台灣）、Odds API 不在啟動時補跑、guarded 不讓單一 job 例外中止排程"""
from datetime import datetime, timedelta

from core.scheduling import guarded, job_specs, next_run_times
from core.timeutil import TPE, UTC


def spec(job_id):
    return next(s for s in job_specs() if s.id == job_id)


def test_twsport_every_30_minutes_taipei():
    now = datetime(2026, 10, 20, 1, 10, tzinfo=UTC)               # 台灣 09:10
    t = next_run_times([spec("odds_twsport")], now)["odds_twsport"]
    assert (t.hour, t.minute) == (9, 33) and t.utcoffset() == timedelta(hours=8)
    nxt = spec("odds_twsport").trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    assert (nxt - t) == timedelta(minutes=30)


def test_oddsapi_four_times_a_day_within_free_quota():
    s = spec("odds_oddsapi")
    assert s.startup_catchup is False                              # 重啟排程器不額外花額度
    t = datetime(2026, 10, 20, 0, 0, tzinfo=TPE)
    fires = []
    for _ in range(8):
        t = s.trigger.get_next_fire_time(None, t + timedelta(seconds=1))
        fires.append((t.hour, t.minute))
    assert fires[:4] == [(0, 40), (6, 40), (12, 40), (18, 40)]
    assert 4 * 3 * 31 <= 500 - 100                                  # 4 次 × 3 credits × 31 天 < 500（留 100 緩衝）


def test_guarded_job_failure_does_not_propagate():
    def boom():
        raise RuntimeError("odds source exploded")
    assert guarded(boom)() is None


def test_market_pricing_every_5_minutes_after_odds_and_predictions():
    """D.2：每 5 分鐘（:01/:06/…）——台彩 :03/:33、Odds API :40 之後幾分鐘內就會定價。"""
    s = spec("market_pricing")
    t = datetime(2026, 10, 20, 9, 3, tzinfo=TPE)
    fires = []
    for _ in range(3):
        t = s.trigger.get_next_fire_time(None, t + timedelta(seconds=1))
        fires.append(t.minute)
    assert fires == [6, 11, 16] and t.utcoffset() == timedelta(hours=8)
    nxt = s.trigger.get_next_fire_time(None, datetime(2026, 10, 20, 12, 40, 30, tzinfo=TPE))
    assert (nxt.hour, nxt.minute) == (12, 41)
