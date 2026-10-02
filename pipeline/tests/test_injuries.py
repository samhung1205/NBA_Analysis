"""傷病：美東時間、15/30 分鐘探測、UTC 儲存、冪等"""
from datetime import datetime

import pytest

from core.sources import injuries as inj
from core.timeutil import UTC


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# ---------------- 純邏輯 ---------------- #

def test_candidates_are_et_naive_not_utc_15min_grid():
    # UTC 22:07 = 美東 17:07（冬令）。若直接傳 UTC 會變成 22:00（差 5 小時）
    got = list(inj.candidate_report_times(utc(2026, 1, 15, 22, 7), interval_minutes=15, lookback_hours=1))
    assert got[:5] == [datetime(2026, 1, 15, 17, 0), datetime(2026, 1, 15, 16, 45), datetime(2026, 1, 15, 16, 30),
                       datetime(2026, 1, 15, 16, 15), datetime(2026, 1, 15, 16, 0)]
    assert all(t.tzinfo is None for t in got)


def test_candidates_summer_offset_is_edt():
    first = next(inj.candidate_report_times(utc(2026, 7, 15, 21, 40), interval_minutes=15))
    assert first == datetime(2026, 7, 15, 17, 30)          # UTC-4


def test_30_minute_interval_skips_quarter_marks():
    got = list(inj.candidate_report_times(utc(2026, 1, 15, 22, 40), interval_minutes=30, lookback_hours=2))
    assert got[:4] == [datetime(2026, 1, 15, 17, 30), datetime(2026, 1, 15, 17, 0),
                       datetime(2026, 1, 15, 16, 30), datetime(2026, 1, 15, 16, 0)]


def test_hourly_interval():
    got = list(inj.candidate_report_times(utc(2026, 1, 15, 22, 40), interval_minutes=60, lookback_hours=3))
    assert got == [datetime(2026, 1, 15, 17, 0), datetime(2026, 1, 15, 16, 0),
                   datetime(2026, 1, 15, 15, 0), datetime(2026, 1, 15, 14, 0)]


def test_legacy_hourly_era_only_probes_whole_hours():
    # 2025-12-22 09:00 ET 之前報告是整點制；探測 :15/:30/:45 只會對到同一份整點報告
    got = list(inj.candidate_report_times(utc(2025, 12, 10, 22, 40), interval_minutes=15, lookback_hours=3))
    assert got and all(t.minute == 0 for t in got)


def test_new_format_boundary_is_included():
    got = list(inj.candidate_report_times(utc(2025, 12, 22, 14, 20), interval_minutes=15, lookback_hours=1))
    assert datetime(2025, 12, 22, 9, 15) in got and datetime(2025, 12, 22, 9, 0) in got
    assert datetime(2025, 12, 22, 8, 45) not in got      # 邊界前（舊制）不探測非整點


def test_dst_fall_back_has_no_duplicate_candidates():
    # 2026-11-01 06:10Z = ET 01:10 EST（回撥後第二次 01:xx）
    got = list(inj.candidate_report_times(utc(2026, 11, 1, 6, 10), interval_minutes=15, lookback_hours=3))
    assert len(got) == len(set(got))


def test_invalid_interval_rejected():
    with pytest.raises(ValueError):
        list(inj.candidate_report_times(utc(2026, 1, 1), interval_minutes=20))


def test_find_latest_report_15_minute_report():
    published = {datetime(2026, 1, 15, 16, 45), datetime(2026, 1, 15, 16, 30)}
    seen = []

    def check(et):
        seen.append(et)
        return et in published

    found = inj.find_latest_report(utc(2026, 1, 15, 22, 7), interval_minutes=15, check=check)
    assert found == datetime(2026, 1, 15, 16, 45)          # 最近一份，而不是整點 16:00
    assert seen == [datetime(2026, 1, 15, 17, 0), datetime(2026, 1, 15, 16, 45)]   # 找到就停
    assert all(t.tzinfo is None for t in seen)


def test_find_latest_report_30_minute_report():
    published = {datetime(2026, 1, 15, 16, 30)}
    assert inj.find_latest_report(utc(2026, 1, 15, 22, 20), interval_minutes=30,
                                  check=published.__contains__) == datetime(2026, 1, 15, 16, 30)
    # 整點網格永遠探測不到 :30 → 證明「不只整點」是必要的
    assert inj.find_latest_report(utc(2026, 1, 15, 22, 20), interval_minutes=60, lookback_hours=3,
                                  check=published.__contains__) is None


def test_find_latest_report_none_when_nothing_published():
    assert inj.find_latest_report(utc(2026, 7, 1, 12), check=lambda et: False) is None


def test_report_time_utc_conversion():
    assert inj.report_time_utc(datetime(2026, 1, 15, 17, 30)) == utc(2026, 1, 15, 22, 30)
    assert inj.report_time_utc(datetime(2026, 7, 15, 17, 30)) == utc(2026, 7, 15, 21, 30)


@pytest.mark.parametrize("raw,expected", [
    ("Brown, Jaylen", "jaylen brown"),
    ("Jaylen Brown", "jaylen brown"),
    ("Jackson Jr., Jaren", "jaren jackson"),
    ("Jaren Jackson Jr.", "jaren jackson"),
    ("Dončić, Luka", "luka doncic"),
    ("O'Neale, Royce", "royce oneale"),
    ("  ", None), (None, None), (float("nan"), None),
])
def test_normalize_player_name(raw, expected):
    assert inj.normalize_player_name(raw) == expected


# ---------------- 需要 DB：冪等 / UTC 儲存 ---------------- #

def _rows(status_brown="Questionable", reason="Injury/Illness - Knee"):
    return [
        {"game_date": "01/15/2026", "matchup": "BOS@NYK", "team": "Boston Celtics",
         "player_name": "Brown, Jaylen", "status": status_brown, "reason": reason},
        {"game_date": "01/15/2026", "matchup": "BOS@NYK", "team": "New York Knicks",
         "player_name": "Jackson Jr., Jaren", "status": "Out", "reason": "Rest"},
        {"game_date": "01/15/2026", "matchup": "BOS@NYK", "team": "Boston Celtics",
         "player_name": "Nobody, Unknown", "status": "Out", "reason": "x"},
        {"game_date": "01/15/2026", "matchup": "BOS@NYK", "team": "Boston Celtics",
         "player_name": "Tatum, Jayson", "status": float("nan"), "reason": "NOT YET SUBMITTED"},
    ]


def test_job_stores_utc_and_is_idempotent(seeded, db):
    from core.jobs.daily import fetch_injuries_job

    now = utc(2026, 1, 15, 22, 7)                       # ET 17:07
    published = {datetime(2026, 1, 15, 17, 0)}
    kw = dict(now=now, check=published.__contains__, fetch=lambda et: _rows())

    assert fetch_injuries_job(**kw) == 2                # Brown + Jackson；Nobody 未對應、Tatum 無狀態
    with db.cursor() as cur:
        cur.execute("SELECT report_time_utc, status FROM injuries ORDER BY id")
        rows = cur.fetchall()
    assert [r["status"] for r in rows] == ["Questionable", "Out"]
    assert all(r["report_time_utc"] == utc(2026, 1, 15, 22, 0) for r in rows)   # ET 17:00 → UTC 22:00

    assert fetch_injuries_job(**kw) == 0                # 同一份 snapshot 重跑：冪等
    assert fetch_injuries_job(**kw) == 0
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM injuries")
        assert cur.fetchone()["n"] == 2


def test_later_report_only_inserts_changes_and_old_rerun_does_not_flip(seeded, db):
    from core.jobs.daily import ingest_injury_rows

    t1, t2 = utc(2026, 1, 15, 22, 0), utc(2026, 1, 15, 22, 15)
    with db.cursor() as cur:
        assert ingest_injury_rows(cur, _rows("Questionable"), t1) == (2, 1)
        # 下一份報告：Brown 變 Out，Jackson 不變 → 只新增 1 列
        assert ingest_injury_rows(cur, _rows("Out"), t2)[0] == 1
        # 補跑較舊的 t1：舊邏輯會拿「最新一筆(Out)」比較而誤判成變化再寫一列
        assert ingest_injury_rows(cur, _rows("Questionable"), t1)[0] == 0
        assert ingest_injury_rows(cur, _rows("Out"), t2)[0] == 0
        cur.execute("SELECT count(*) AS n FROM injuries")
        assert cur.fetchone()["n"] == 3


def test_job_without_report_records_warn_heartbeat_not_crash(seeded, db):
    from core.jobs.daily import fetch_injuries_job

    assert fetch_injuries_job(now=utc(2026, 7, 1, 12), check=lambda et: False) is None
    with db.cursor() as cur:
        cur.execute("SELECT last_status, last_success_at FROM data_sources WHERE source_key='nbainjuries'")
        r = cur.fetchone()
    assert r["last_status"] == "warn" and r["last_success_at"] is None


def test_job_parse_failure_records_error_and_does_not_raise(seeded, db):
    from core.jobs.daily import fetch_injuries_job

    def boom(et):
        raise RuntimeError("PDF layout changed")

    assert fetch_injuries_job(now=utc(2026, 1, 15, 22, 7), check=lambda et: True, fetch=boom) is None
    with db.cursor() as cur:
        cur.execute("SELECT last_status, last_error FROM data_sources WHERE source_key='nbainjuries'")
        r = cur.fetchone()
    assert r["last_status"] == "error" and "PDF layout changed" in r["last_error"]
