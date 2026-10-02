"""傷病快照寫入（冪等/併發/原子性）、injuries 最新狀態（球員消失）、歷史回填（續傳/冪等/不寫 injuries）"""
import threading
from datetime import datetime

import pytest

from core import injury_asof as ia
from core import injury_history as ih
from core.jobs import injury_backfill as ibf
from core.jobs.daily import NOT_LISTED_REASON, ingest_injury_rows
from core.sources.injuries import ReportNotPublished
from core.timeutil import UTC, et_naive_to_utc
from tests.helpers import count, utc

NYS_BOS = {"game_date": "01/15/2026", "matchup": "BOS@NYK", "team": "Boston Celtics", "player_name": float("nan"),
           "status": float("nan"), "reason": "NOT YET SUBMITTED"}


def r(team, name, status, reason="Knee", gdate="01/15/2026", matchup="BOS@NYK"):
    return {"game_date": gdate, "matchup": matchup, "team": team, "player_name": name, "status": status,
            "reason": reason}


BROWN_OUT = r("Boston Celtics", "Brown, Jaylen", "Out")
TATUM_Q = r("Boston Celtics", "Tatum, Jayson", "Questionable")
JACKSON_OUT = r("New York Knicks", "Jackson Jr., Jaren", "Out", "Rest")
T1, T2, T3 = utc(2026, 1, 15, 20), utc(2026, 1, 15, 22), utc(2026, 1, 16, 0)


def parse(db, rows, when):
    with db.cursor() as cur:
        teams, players = ih.load_directories(cur)
    return ih.parse_report(rows, when, teams, players)


def pid(db, name):
    with db.cursor() as cur:
        cur.execute("SELECT id FROM players WHERE name = %s", (name,))
        return cur.fetchone()["id"]


def latest_legacy(db, name):
    """與 src/db/queries.ts getInjuriesByTpeDate 相同的「每位球員最新一筆」語意。"""
    with db.cursor() as cur:
        cur.execute("""SELECT i.status, i.reason FROM injuries i WHERE i.player_id = %s AND i.report_time_utc =
                       (SELECT MAX(i2.report_time_utc) FROM injuries i2 WHERE i2.player_id = i.player_id)""",
                    (pid(db, name),))
        return cur.fetchone()


# ---------------- 快照寫入 ---------------- #

def test_ingest_snapshot_is_idempotent(seeded, db):
    p = parse(db, [BROWN_OUT, TATUM_Q, JACKSON_OUT], T1)
    ids = []
    for _ in range(3):
        with db.transaction() as cur:
            ids.append(ih.ingest_snapshot(cur, p))
    assert ids[0] is not None and ids[1:] == [None, None]
    assert count(db, "injury_reports") == 1 and count(db, "injury_report_entries") == 3
    with db.cursor() as cur:
        cur.execute("SELECT n_rows, n_entries, coverage FROM injury_reports")
        row = cur.fetchone()
    assert (row["n_rows"], row["n_entries"]) == (3, 3) and "2026-01-15" in row["coverage"]


def test_concurrent_ingest_of_same_report_writes_once(seeded, db):
    p = parse(db, [BROWN_OUT, TATUM_Q, JACKSON_OUT], T1)
    barrier, results, errors = threading.Barrier(2), [], []

    def work():
        try:
            with db.transaction() as cur:
                barrier.wait(timeout=10)
                results.append(ih.ingest_snapshot(cur, p))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert not errors
    assert sorted(x is None for x in results) == [False, True]       # 恰好一個贏
    assert count(db, "injury_reports") == 1 and count(db, "injury_report_entries") == 3


def test_snapshot_write_is_atomic(seeded, db):
    p = parse(db, [BROWN_OUT], T1)
    p.entries[0].game_date = "not-a-date"                             # 讓 entries 的 INSERT 失敗
    with pytest.raises(Exception):
        with db.transaction() as cur:
            ih.ingest_snapshot(cur, p)
    assert count(db, "injury_reports") == 0 and count(db, "injury_report_entries") == 0


def test_older_report_ingested_after_newer_does_not_change_asof_result(seeded, db):
    for when, rows in ((T3, [BROWN_OUT]), (T1, [r("Boston Celtics", "Brown, Jaylen", "Questionable")])):   # 先新後舊
        with db.transaction() as cur:
            ih.ingest_snapshot(cur, parse(db, rows, when))
    with db.cursor() as cur:
        idx = ia.load_index(cur)
    tip = utc(2026, 1, 16, 1)
    st = ia.team_state_asof(idx, team_abbr="BOS", game_date=datetime(2026, 1, 15).date(), cutoff_utc=tip)
    assert st.report_time_utc == T3 and st.status_of(pid(db, "Jaylen Brown")) == "Out"
    early = ia.team_state_asof(idx, team_abbr="BOS", game_date=datetime(2026, 1, 15).date(), cutoff_utc=utc(2026, 1, 15, 23))
    assert early.report_time_utc == T1 and early.status_of(pid(db, "Jaylen Brown")) == "Questionable"


def test_reresolve_unmatched_after_player_appears(seeded, db):
    with db.transaction() as cur:
        ih.ingest_snapshot(cur, parse(db, [r("Boston Celtics", "Newguy, Rookie", "Out")], T1))
    assert count(db, "injury_report_entries", "player_id IS NULL") == 1
    with db.cursor() as cur:
        db.upsert_player(cur, nba_player_id=999, name="Rookie Newguy", team_id=seeded["BOS"])
        assert ih.reresolve_unmatched(cur) == 1
    assert count(db, "injury_report_entries", "player_id IS NULL") == 0
    assert count(db, "injury_reports", "n_unmatched = 0") == 1


# ---------------- injuries（API 最新狀態）：球員消失 ---------------- #

def test_disappeared_player_becomes_available_in_latest_state(seeded, db):
    with db.cursor() as cur:
        ingest_injury_rows(cur, [BROWN_OUT, JACKSON_OUT], T1)
        assert latest_legacy(db, "Jaylen Brown")["status"] == "Out"
        # 之後的報告：BOS 已申報（Tatum 被列），Brown 不再出現
        w, _ = ingest_injury_rows(cur, [TATUM_Q, JACKSON_OUT], T2)
    got = latest_legacy(db, "Jaylen Brown")
    assert got == {"status": "Available", "reason": NOT_LISTED_REASON}   # 不再永久 Out
    assert latest_legacy(db, "Jaren Jackson Jr.")["status"] == "Out"      # 仍被列出者不變
    assert w == 2                                                         # Tatum 新增 + Brown 轉 Available


def test_disappearance_is_idempotent_and_does_not_repeat(seeded, db):
    with db.cursor() as cur:
        ingest_injury_rows(cur, [BROWN_OUT, JACKSON_OUT], T1)
        for _ in range(3):
            ingest_injury_rows(cur, [TATUM_Q, JACKSON_OUT], T2)
        ingest_injury_rows(cur, [TATUM_Q, JACKSON_OUT], T3)               # 更晚的報告仍不再列 Brown：不重複寫
    assert count(db, "injuries", "player_id = %s", (pid(db, "Jaylen Brown"),)) == 2     # Out, Available


def test_not_yet_submitted_team_keeps_previous_state(seeded, db):
    with db.cursor() as cur:
        ingest_injury_rows(cur, [BROWN_OUT, JACKSON_OUT], T1)
        ingest_injury_rows(cur, [NYS_BOS, JACKSON_OUT], T2)               # BOS 尚未申報：沒有資訊，不是康復
    assert latest_legacy(db, "Jaylen Brown")["status"] == "Out"


def test_team_absent_from_report_is_untouched(seeded, db):
    with db.cursor() as cur:
        ingest_injury_rows(cur, [BROWN_OUT, JACKSON_OUT], T1)
        ingest_injury_rows(cur, [r("Los Angeles Lakers", "Someone, Else", "Out", matchup="LAL@NOP")], T2)
    assert latest_legacy(db, "Jaylen Brown")["status"] == "Out"           # BOS 不在這份報告涵蓋範圍 → 無資訊


def test_explicit_available_status_is_not_rewritten(seeded, db):
    with db.cursor() as cur:
        ingest_injury_rows(cur, [r("Boston Celtics", "Brown, Jaylen", "Available"), JACKSON_OUT], T1)
        ingest_injury_rows(cur, [TATUM_Q, JACKSON_OUT], T2)
    assert count(db, "injuries", "player_id = %s", (pid(db, "Jaylen Brown"),)) == 1


def test_multi_date_report_uses_earliest_date_per_team_for_latest_state(seeded, db):
    rows = [r("Boston Celtics", "Brown, Jaylen", "Out", gdate="01/15/2026"),
            r("Boston Celtics", "Brown, Jaylen", "Probable", gdate="01/16/2026", matchup="BOS@MIA"),
            JACKSON_OUT]
    with db.cursor() as cur:
        ingest_injury_rows(cur, rows, T1)
    assert latest_legacy(db, "Jaylen Brown")["status"] == "Out"
    assert count(db, "injuries", "player_id = %s", (pid(db, "Jaylen Brown"),)) == 1
    assert count(db, "injury_report_entries", "player_id = %s", (pid(db, "Jaylen Brown"),)) == 2   # 快照保留兩個日期


def test_older_report_arriving_late_cannot_override_newer_latest_state(seeded, db):
    with db.cursor() as cur:
        ingest_injury_rows(cur, [TATUM_Q, JACKSON_OUT], T2)               # 較新：BOS 沒列 Brown
        written, _ = ingest_injury_rows(cur, [BROWN_OUT, JACKSON_OUT], T1)   # 較舊的補抓
    assert written == 0                                                   # 舊報告不動「最新狀態」表
    assert latest_legacy(db, "Jaylen Brown") is None                      # 沒有被舊報告的 Out 蓋過
    assert latest_legacy(db, "Jayson Tatum")["status"] == "Questionable"
    assert count(db, "injury_reports") == 2                               # 但快照完整保存（歷史 as-of 用）
    with db.cursor() as cur:
        idx = ia.load_index(cur)
    st = ia.team_state_asof(idx, team_abbr="BOS", game_date=datetime(2026, 1, 15).date(), cutoff_utc=utc(2026, 1, 15, 21))
    assert st.report_time_utc == T1 and st.status_of(pid(db, "Jaylen Brown")) == "Out"


def test_legacy_insert_is_concurrency_safe(seeded, db):
    barrier, out = threading.Barrier(2), []

    def work():
        with db.cursor() as cur:
            barrier.wait(timeout=10)
            out.append(db.insert_injury_if_changed(
                cur, report_time_utc=T1, player_id=pid(db, "Jaylen Brown"), team_id=seeded["BOS"], game_id=None,
                status="Out", reason="Knee", source="nba_official"))

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert count(db, "injuries") == 1 and sorted(out) == [False, True]


def test_unique_index_exists_on_injuries(db):
    with db.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_injuries_report_player'"
                    " AND schemaname = current_schema()")
        assert cur.fetchone() is not None


# ---------------- 歷史回填 ---------------- #

def test_plan_report_times_old_format_hourly_before_tip():
    tip = utc(2024, 2, 16, 1, 0)                                          # 2024-02-15 20:00 ET
    plan = ibf.plan_report_times([tip], now=utc(2026, 1, 1))
    assert datetime(2024, 2, 14, 17, 0) in plan and datetime(2024, 2, 15, 10, 0) in plan      # 錨點
    assert [datetime(2024, 2, 15, h) for h in (17, 18, 19)] == [t for t in plan if t.day == 15 and t.hour >= 17]
    assert datetime(2024, 2, 15, 20, 0) not in plan                       # 開賽時刻本身不取（嚴格早於）
    assert all(t.minute == 0 for t in plan)                               # 舊制只有整點


def test_plan_report_times_new_format_adds_quarter_hour_and_never_future():
    tip = utc(2026, 1, 16, 1, 0)                                          # 2026-01-15 20:00 ET
    plan = ibf.plan_report_times([tip], now=utc(2026, 1, 1))
    assert plan == []                                                     # 未來比賽：不回填未發生的報告
    plan = ibf.plan_report_times([tip], now=utc(2026, 2, 1))
    assert {datetime(2026, 1, 15, 19, m) for m in (0, 15, 30, 45)} <= set(plan)
    assert datetime(2026, 1, 15, 20, 0) not in plan and datetime(2026, 1, 15, 19, 45) in plan


def test_plan_report_times_dedupes_and_respects_earliest():
    tips = [utc(2021, 10, 20, 2), utc(2021, 10, 20, 2, 30), utc(2021, 10, 20, 2, 0)]
    plan = ibf.plan_report_times(tips, now=utc(2022, 1, 1))
    assert plan == sorted(set(plan)) and min(plan) >= ibf.EARLIEST_ET
    assert ibf.plan_report_times([utc(2020, 1, 1)], now=utc(2022, 1, 1)) == []


def _plan(n=4):
    return [datetime(2024, 2, 15, 12 + i, 0) for i in range(n)]


def _rows_for(et):
    return [r("Boston Celtics", "Brown, Jaylen", "Out", gdate=f"{et:%m/%d/%Y}")]


def test_backfill_ingests_is_idempotent_and_never_touches_injuries_table(seeded, db):
    plan = _plan()
    s1 = ibf.run(plan, fetch=_rows_for, sleep=lambda s: None)
    assert (s1["ingested"], s1["errors"], s1["missing"]) == (4, 0, 0)
    assert count(db, "injury_reports") == 4 and count(db, "injury_report_entries") == 4
    assert count(db, "injuries") == 0                                     # 回填不影響「今日傷病」顯示
    s2 = ibf.run(plan, fetch=_rows_for, sleep=lambda s: None)
    assert (s2["ingested"], s2["todo"], s2["already"]) == (0, 0, 4)
    assert count(db, "injury_reports") == 4 and count(db, "injury_report_entries") == 4


def test_backfill_interrupted_then_resumed(seeded, db):
    plan = _plan(6)
    calls = {"n": 0}

    def flaky(et):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("PDF parse crash")
        return _rows_for(et)

    s1 = ibf.run(plan, fetch=flaky, sleep=lambda s: None)
    assert (s1["ingested"], s1["errors"]) == (5, 1) and len(s1["failed"]) == 1      # 單份失敗不中止
    s2 = ibf.run(plan, fetch=_rows_for, sleep=lambda s: None)                        # 續傳：只補失敗那份
    assert (s2["ingested"], s2["todo"]) == (1, 1)
    assert count(db, "injury_reports") == 6 and count(db, "source_fetch_log", "status = 'ok'") == 6

    s3 = ibf.run(plan, fetch=_rows_for, sleep=lambda s: None)
    assert s3["ingested"] == 0


def test_backfill_limit_simulates_interrupt_and_resume_matches_full_run(seeded, db):
    plan = _plan(6)
    ibf.run(plan, limit=2, fetch=_rows_for, sleep=lambda s: None)
    assert count(db, "injury_reports") == 2
    ibf.run(plan, fetch=_rows_for, sleep=lambda s: None)
    assert count(db, "injury_reports") == 6 and count(db, "injury_report_entries") == 6


def test_backfill_missing_reports_logged_and_not_reprobed(seeded, db):
    plan = _plan(3)
    seen = []

    def fetch(et):
        seen.append(et)
        if et.hour == 13:
            raise ReportNotPublished("403")
        return _rows_for(et)

    s1 = ibf.run(plan, fetch=fetch, sleep=lambda s: None)
    assert (s1["ingested"], s1["missing"]) == (2, 1) and seen.count(plan[1]) == 2    # 先重試一次才算 missing
    seen.clear()
    s2 = ibf.run(plan, fetch=fetch, sleep=lambda s: None)
    assert seen == [] and s2["todo"] == 0                                             # 不再重探
    ibf.run(plan, fetch=_rows_for, retry_missing=True, sleep=lambda s: None)
    assert count(db, "injury_reports") == 3


def test_backfill_shards_partition_the_plan(seeded, db):
    plan = _plan(7)
    seen = []
    for i in range(3):
        ibf.run(plan, shard=(i, 3), fetch=lambda et: (seen.append(et), _rows_for(et))[1], sleep=lambda s: None)
    assert sorted(seen) == plan and count(db, "injury_reports") == 7


def test_backfill_report_time_is_stored_as_utc_from_et(seeded, db):
    ibf.run([datetime(2024, 2, 15, 17, 0)], fetch=_rows_for, sleep=lambda s: None)
    with db.cursor() as cur:
        cur.execute("SELECT report_time_utc FROM injury_reports")
        assert cur.fetchone()["report_time_utc"] == utc(2024, 2, 15, 22, 0)         # 17:00 EST = 22:00 UTC
