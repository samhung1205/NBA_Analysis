"""UTC ↔ America/New_York ↔ Asia/Taipei 邊界"""
from datetime import date, datetime, timedelta

import pytest

from core.timeutil import (
    TPE, UTC, et_date, et_dates_between, et_dates_for_refresh, et_naive_to_utc, in_window,
    parse_utc, refresh_window_utc, to_et_naive, tpe_date, tpe_day_bounds_utc,
)


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_taipei_morning_still_sees_previous_et_game_day():
    # 台灣 10/3 10:00 = UTC 10/3 02:00 = 美東 10/2 22:00（夏令）：台灣的「今天」仍有 ET 10/2 的賽事
    now = utc(2026, 10, 3, 2, 0)
    assert tpe_date(now) == date(2026, 10, 3)
    assert et_date(now) == date(2026, 10, 2)
    dates = et_dates_for_refresh(now)
    assert date(2026, 10, 2) in dates          # 舊邏輯（把台灣日期字串當 ET 日期）會漏掉這天
    assert date(2026, 10, 3) in dates and date(2026, 10, 4) in dates
    # 舊邏輯重現：只查 [台灣今天, 台灣明天] 當 ET 日期
    old_logic = {tpe_date(now), tpe_date(now) + timedelta(days=1)}
    assert date(2026, 10, 2) not in old_logic


def test_late_et_game_belongs_to_et_day_but_taipei_next_day():
    tip = utc(2026, 10, 3, 2, 30)                 # ET 10/2 22:30，台灣 10/3 10:30
    assert et_date(tip) == date(2026, 10, 2)
    assert tpe_date(tip) == date(2026, 10, 3)
    start, end = tpe_day_bounds_utc(date(2026, 10, 3))   # 台灣 10/3 這一天
    assert in_window(tip, start, end)


def test_tpe_day_bounds_utc():
    s, e = tpe_day_bounds_utc(date(2026, 10, 3))
    assert (s, e) == (utc(2026, 10, 2, 16), utc(2026, 10, 3, 16))


def test_in_window_is_half_open_and_uses_game_time_not_date_strings():
    s, e = utc(2026, 1, 1), utc(2026, 1, 2)
    assert in_window(s, s, e) and not in_window(e, s, e)
    assert not in_window(None, s, e)
    # aware 非 UTC 時區也以實際時間點比較
    assert in_window(datetime(2026, 1, 1, 8, 0, tzinfo=TPE), s, e)   # = UTC 00:00


@pytest.mark.parametrize("now,expect_tomorrow_end", [
    (utc(2026, 10, 2, 3, 28), utc(2026, 10, 3, 16)),    # 台灣 10/2 → 明天 10/3 結束 = UTC 10/3 16:00
    (utc(2026, 10, 2, 15, 59), utc(2026, 10, 3, 16)),   # 台灣 23:59（仍是 10/2）
    (utc(2026, 10, 2, 16, 0), utc(2026, 10, 4, 16)),    # 台灣 10/3 00:00 → 明天變 10/4
])
def test_refresh_window_end_is_end_of_taipei_tomorrow(now, expect_tomorrow_end):
    start, end = refresh_window_utc(now)
    assert end == expect_tomorrow_end
    assert start == now - timedelta(hours=24)


def test_et_dates_between_handles_dst_fall_back():
    # 2026-11-01 美東回撥（EDT→EST）：該 ET 日有 25 小時
    dates = et_dates_between(utc(2026, 11, 1, 3), utc(2026, 11, 3, 3))
    assert dates == [date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)]


def test_et_offset_changes_with_dst():
    assert et_date(utc(2026, 7, 1, 3, 59)) == date(2026, 6, 30)    # EDT: UTC-4
    assert et_date(utc(2026, 7, 1, 4, 0)) == date(2026, 7, 1)
    assert et_date(utc(2026, 1, 1, 4, 59)) == date(2025, 12, 31)   # EST: UTC-5
    assert et_date(utc(2026, 1, 1, 5, 0)) == date(2026, 1, 1)


def test_to_et_naive_is_not_utc_and_roundtrips():
    assert to_et_naive(utc(2026, 1, 15, 22, 30)) == datetime(2026, 1, 15, 17, 30)   # EST
    assert to_et_naive(utc(2026, 7, 15, 21, 30)) == datetime(2026, 7, 15, 17, 30)   # EDT
    assert to_et_naive(utc(2026, 1, 15, 22, 30)).tzinfo is None
    for t in (utc(2026, 1, 15, 22, 30), utc(2026, 7, 15, 21, 30), utc(2026, 3, 8, 12, 0)):
        assert et_naive_to_utc(to_et_naive(t)) == t


def test_et_naive_to_utc_spring_forward_and_fall_back():
    assert et_naive_to_utc(datetime(2026, 3, 8, 12, 0)) == utc(2026, 3, 8, 16)    # 已是 EDT
    assert et_naive_to_utc(datetime(2026, 3, 8, 1, 0)) == utc(2026, 3, 8, 6)      # 仍是 EST
    assert et_naive_to_utc(datetime(2026, 11, 1, 1, 30)) == utc(2026, 11, 1, 5, 30)  # 重複小時取第一次(EDT)


def test_parse_utc():
    assert parse_utc("2026-10-03T23:00:00Z") == utc(2026, 10, 3, 23)
    assert parse_utc("2026-10-03T19:00:00-04:00") == utc(2026, 10, 3, 23)
    assert parse_utc(None) is None and parse_utc("") is None
    assert parse_utc(datetime(2026, 1, 1)).tzinfo is not None
