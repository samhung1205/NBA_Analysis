"""scheduler-efficiency-v1：idle 時不做重工作，有事時行為完全不變。

純函式 / 假 cursor 的測試不連資料庫；標記 db 的測試使用暫存 schema（不碰 public 的正式資料）。
整份檔案封鎖對外網路：任何測試誤打外部 API（含 The Odds API）會立刻失敗，不會花額度。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from core.production import activity
from core.production.activity import (CADENCE_GRACE, INJURY_WINDOW, injury_poll_decision, injury_tier)

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)


@pytest.fixture(autouse=True)
def _no_network_and_clean_logs(monkeypatch):
    import requests

    def boom(*a, **k):
        raise AssertionError("測試不得對外連線（The Odds API / NBA / 台彩）")
    monkeypatch.setattr(requests.sessions.Session, "request", boom)
    activity.reset_observability()


# ------------------------------------------------------------------ #
# 傷病：tier 邊界 / cadence / DST                                        #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("until,tier,minutes", [
    (None, "idle", None),
    (timedelta(0), "idle", None),
    (timedelta(seconds=-1), "idle", None),
    (timedelta(seconds=1), "0-3h", 15),
    (2 * H, "0-3h", 15),
    (3 * H, "0-3h", 15),                                   # 剛好 3h → 15 分鐘 tier
    (3 * H + timedelta(seconds=1), "3-12h", 60),
    (8 * H, "3-12h", 60),
    (12 * H, "3-12h", 60),                                 # 剛好 12h → 60 分鐘 tier
    (12 * H + timedelta(seconds=1), "12-36h", 120),
    (24 * H, "12-36h", 120),
    (36 * H, "12-36h", 120),                               # 剛好 36h 仍在窗口內
    (36 * H + timedelta(seconds=1), "idle", None),
    (72 * H, "idle", None),
])
def test_injury_tier_boundaries(until, tier, minutes):
    assert injury_tier(until) == (tier, minutes)


def test_no_game_or_beyond_window_never_runs():
    d = injury_poll_decision(None, None, NOW)
    assert (d.run, d.reason) == (False, "idle_no_upcoming_game")
    d = injury_poll_decision(NOW + 37 * H, None, NOW)
    assert (d.run, d.reason) == (False, "outside_injury_window")
    d = injury_poll_decision(NOW + INJURY_WINDOW + timedelta(seconds=1), NOW - 10 * H, NOW)
    assert d.run is False


@pytest.mark.parametrize("hours,interval", [(24, 120), (8, 60), (2, 15)])
def test_cadence_per_tier(hours, interval):
    nxt = NOW + hours * H
    assert injury_poll_decision(nxt, None, NOW).run is True                                    # 從沒抓過 → 抓
    for ago in (1, interval // 2, interval - 3):
        d = injury_poll_decision(nxt, NOW - timedelta(minutes=ago), NOW)
        assert (d.run, d.reason, d.interval_min) == (False, "cadence_not_due", interval)
    # 到期；網格容許誤差（上一輪晚幾秒結束不會多等一整格）
    assert injury_poll_decision(nxt, NOW - timedelta(minutes=interval), NOW).run is True
    assert injury_poll_decision(nxt, NOW - timedelta(minutes=interval) + CADENCE_GRACE, NOW).run is True
    assert injury_poll_decision(nxt, NOW - timedelta(minutes=interval) + CADENCE_GRACE + timedelta(seconds=1), NOW).run is False


def test_tier_tightens_as_game_approaches():
    last = NOW - timedelta(minutes=45)
    assert injury_poll_decision(NOW + 8 * H, last, NOW).run is False        # 60 分 tier：45 分鐘還沒到
    assert injury_poll_decision(NOW + 2 * H, last, NOW).run is True         # 15 分 tier：45 分鐘已到


def test_dst_uses_real_elapsed_time_not_wall_clock():
    """美東 2026-11-01 02:00 夏令結束（時鐘撥回 1 小時）。00:30 EDT → 12:00 EST 實際相隔 12.5 小時（不是 11.5）。"""
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 0, 30, tzinfo=et)
    game = datetime(2026, 11, 1, 12, 0, tzinfo=et)
    assert (game - now) == timedelta(hours=11, minutes=30)               # 同一 tz 的 aware 相減是牆上時間差（陷阱）
    d = injury_poll_decision(game, None, now)                             # 我們以 UTC 瞬間計算
    assert d.hours_to_next == 12.5 and d.tier == "12-36h" and d.interval_min == 120
    # 春季：2026-03-08 02:00 EST → 03:00 EDT（少 1 小時）。00:30 EST → 12:00 EDT 實際相隔 10.5 小時
    now = datetime(2026, 3, 8, 0, 30, tzinfo=et)
    game = datetime(2026, 3, 8, 12, 0, tzinfo=et)
    d = injury_poll_decision(game, None, now)
    assert d.hours_to_next == 10.5 and d.tier == "3-12h" and d.interval_min == 60


# ------------------------------------------------------------------ #
# 觀測：log 不洗版、計數仍累計                                              #
# ------------------------------------------------------------------ #

def test_skip_log_is_rate_limited_but_counted(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="core.production.activity"):
        for _ in range(50):
            activity.log_skip("pricing", "no_fresh_inputs")
        activity.log_skip("pricing", "no_odds_snapshots")           # 原因改變 → 再記一次
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 2 and "[scheduler-efficiency] pricing skipped: no_fresh_inputs" in infos[0].getMessage()
    assert activity.COUNTERS[("pricing", "no_fresh_inputs")] == 50


# ------------------------------------------------------------------ #
# 假 cursor：驗證 gate 的 SQL 決策（不連 DB）                               #
# ------------------------------------------------------------------ #

class FakeCur:
    def __init__(self, *results):
        self.results, self.sql, self.rowcount = list(results), [], 0

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self._cur = self.results.pop(0) if self.results else []

    def fetchone(self):
        r = self._cur
        return (r[0] if isinstance(r, list) and r else r) or None

    def fetchall(self):
        return self._cur


def test_pricing_gate_no_games_no_odds_unchanged_and_new_inputs():
    from core.sizing.job import pricing_gate
    g = pricing_gate(FakeCur([]), NOW, None)
    assert (g.run_pricing, g.run_sizing, g.reason) == (False, False, "no_games_in_window")
    g = pricing_gate(FakeCur([{"id": 1}], {"o": 0, "p": 7}), NOW, None)
    assert (g.run_pricing, g.run_sizing, g.reason) == (False, False, "no_odds_snapshots")
    first = pricing_gate(FakeCur([{"id": 1}, {"id": 2}], {"o": 10, "p": 7}), NOW, None)
    assert (first.run_pricing, first.run_sizing, first.reason) == (True, True, "active")
    same = pricing_gate(FakeCur([{"id": 1}, {"id": 2}], {"o": 10, "p": 7}), NOW + timedelta(minutes=5), first.key)
    assert (same.run_pricing, same.run_sizing, same.reason) == (False, True, "no_fresh_inputs")   # sizing 隨時間變，照跑
    for new in ({"o": 11, "p": 7}, {"o": 10, "p": 8}):                                           # 新盤口 / 新預測
        g = pricing_gate(FakeCur([{"id": 1}, {"id": 2}], new), NOW, first.key)
        assert g.run_pricing is True
    g = pricing_gate(FakeCur([{"id": 1}, {"id": 2}, {"id": 3}], {"o": 10, "p": 7}), NOW, first.key)  # 比賽集合改變
    assert g.run_pricing is True


def test_paper_has_work_only_for_due_candidates_or_open_bets():
    from core.execution.ledger import paper_has_work
    assert paper_has_work(FakeCur([], None), NOW) is False                                # 無候選、無未結算
    assert paper_has_work(FakeCur([{"id": 1, "date_utc": NOW, "status": "scheduled"}]), NOW) is True   # T-60 候選
    assert paper_has_work(FakeCur([], {"?column?": 1}), NOW) is True                    # 未結算 paper bet


def test_retrain_state_decisions():
    from datetime import timedelta as td
    st = activity.retrain_state(FakeCur({"n": 6605}), NOW, 6605, buffer=td(hours=4))
    assert (st.has_new_data, st.reason) == (False, "no_new_training_data")
    st = activity.retrain_state(FakeCur({"n": 6612}), NOW, 6605, buffer=td(hours=4))
    assert (st.has_new_data, st.reason) == (True, "new_training_data")
    st = activity.retrain_state(FakeCur({"n": 1}), NOW, None, buffer=td(hours=4))      # 無法判斷 → 保守：重訓
    assert st.has_new_data is True


def test_scheduled_wiring_and_frozen_triggers():
    """觸發時間不變（只是醒來後自己判斷）；Odds API 排程與每日賽程同步不被改動。"""
    from core.scheduling import job_specs
    specs = {s.id: s for s in job_specs()}
    assert specs["injuries_peak"].func.__name__ == "injury_poll_job" == specs["injuries_offpeak"].func.__name__
    assert str(specs["odds_oddsapi"].trigger).startswith("cron[") and "minute='40'" in str(specs["odds_oddsapi"].trigger)
    assert "hour='12'" in str(specs["daily_schedule_scores"].trigger)
    assert len(specs) == 13


# ------------------------------------------------------------------ #
# DB（暫存 schema）                                                      #
# ------------------------------------------------------------------ #

def add_game(db, ids, when, *, status="scheduled", stage="regular", nba="g", home="BOS", away="NYK", pts=False,
             season="2026-27"):
    with db.cursor() as cur:
        cur.execute("""INSERT INTO games (nba_game_id, season, season_stage, date_utc, home_team_id, away_team_id, status,
                                          home_pts, away_pts)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (nba, season, stage, when, ids[home], ids[away], status, 110 if pts else None, 100 if pts else None))
        return cur.fetchone()["id"]


def test_next_eligible_game_uses_existing_eligibility(db, seeded):
    with db.cursor() as cur:
        assert activity.next_eligible_game(cur, NOW) is None
    add_game(db, seeded, NOW - H, nba="started")                              # 已開賽 → 不算
    add_game(db, seeded, NOW + 2 * H, nba="pre", stage="preseason")           # 季前賽 → 不是 production-eligible
    add_game(db, seeded, NOW + 3 * H, nba="done", status="final", pts=True)   # final → 不算
    with db.cursor() as cur:
        assert activity.next_eligible_game(cur, NOW) is None
    add_game(db, seeded, NOW + 30 * H, nba="far")
    add_game(db, seeded, NOW + 9 * H, nba="near")                             # 多場比賽：最近的決定 cadence
    add_game(db, seeded, NOW + 20 * H, nba="mid")
    with db.cursor() as cur:
        assert activity.next_eligible_game(cur, NOW) == NOW + 9 * H
        assert activity.has_eligible_game_between(cur, NOW, NOW + 10 * H) is True
        assert activity.has_eligible_game_between(cur, NOW + 10 * H, NOW + 19 * H) is False


class FetchSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, now, *, expected_interval_min):
        self.calls.append(expected_interval_min)
        return 0


def test_injury_job_no_game_no_external_call(db, seeded):
    from core.jobs.daily import injury_poll_job
    spy = FetchSpy()
    assert injury_poll_job(NOW, fetch_job=spy) is None and spy.calls == []
    add_game(db, seeded, NOW - H, nba="started-only")                         # 只有已開賽的比賽 → 不輪詢
    assert injury_poll_job(NOW, fetch_job=spy) is None and spy.calls == []
    add_game(db, seeded, NOW + 48 * H, nba="too-far")                         # 窗口外
    assert injury_poll_job(NOW, fetch_job=spy) is None and spy.calls == []


def test_injury_job_cadence_follows_nearest_game_and_syncs_expected_interval(db, seeded):
    from core.jobs.daily import injury_poll_job
    add_game(db, seeded, NOW + 24 * H, nba="a")
    spy = FetchSpy()
    injury_poll_job(NOW, fetch_job=spy)
    assert spy.calls == [120]
    with db.cursor() as cur:        # fetch 本身會寫心跳；這裡以 spy 代替，手動寫入「剛抓過」
        db.heartbeat(cur, source_key="nbainjuries", display_name="x", category="injury", status="ok", expected_interval_min=120)
        cur.execute("UPDATE data_sources SET last_attempt_at = %s WHERE source_key = 'nbainjuries'", (NOW,))
    injury_poll_job(NOW + timedelta(minutes=60), fetch_job=spy)
    assert spy.calls == [120]                                                 # 120 分鐘內不重抓
    add_game(db, seeded, NOW + 9 * H, nba="b")                                # 最近的比賽變成 9h → 60 分鐘 tier
    injury_poll_job(NOW + timedelta(minutes=60), fetch_job=spy)
    assert spy.calls == [120, 60]
    with db.cursor() as cur:
        cur.execute("SELECT expected_interval_min FROM data_sources WHERE source_key = 'nbainjuries'")
        assert cur.fetchone()["expected_interval_min"] == 60                 # 系統狀態頁的「過期」門檻跟著 tier 走


def test_predict_final_and_refresh_skip_artifact_load_when_no_game_in_window(db, seeded, monkeypatch):
    from core.production import artifact as art_mod
    from core.production import predict

    def boom(*a, **k):
        raise AssertionError("idle 時不應載入 artifact")
    monkeypatch.setattr(art_mod, "load_current", boom)
    for kind in ("final", "refresh"):
        rep = predict.predict_upcoming_games_job(kind, NOW)
        assert rep.candidates == 0 and "scheduler-efficiency" in (rep.note or "")
    add_game(db, seeded, NOW + 5 * H, nba="x")       # 5h：不在 final 視窗（≤75 分），但在 refresh 視窗 → 要往下走（會碰 artifact）
    with pytest.raises(AssertionError):
        predict.predict_upcoming_games_job("refresh", NOW)
    predict.predict_upcoming_games_job("final", NOW)  # final 視窗仍空 → 仍略過


def test_scheduled_pricing_skips_and_runs_correctly(db, seeded, monkeypatch):
    import core.pricing.job as pj
    import core.sizing.job as sj
    calls = {"pricing": 0, "sizing": 0}

    class Rep:
        errors: list = []

    def fake_pricing(now=None, **k):
        calls["pricing"] += 1
        return Rep()
    monkeypatch.setattr(pj, "pricing_job", fake_pricing)
    monkeypatch.setattr(sj, "sizing_job", lambda now=None, **k: calls.__setitem__("sizing", calls["sizing"] + 1))
    monkeypatch.setattr(sj, "_LAST_PRICED_KEY", None)
    monkeypatch.setattr(sj, "now_utc", lambda: NOW)
    sj.pricing_and_sizing_scheduled()                       # 沒有比賽
    assert calls == {"pricing": 0, "sizing": 0}
    gid = add_game(db, seeded, NOW + 5 * H, nba="p1")
    sj.pricing_and_sizing_scheduled()                       # 有比賽、沒有盤口
    assert calls == {"pricing": 0, "sizing": 0}
    with db.cursor() as cur:
        cur.execute("""INSERT INTO odds_snapshots (fetched_at, game_id, source, market, home_odds, away_odds)
                       VALUES (%s, %s, 'twsport', 'ml', 1.9, 1.9)""", (NOW - timedelta(minutes=10), gid))
    sj.pricing_and_sizing_scheduled()                       # 新有效輸入 → 既有定價 + sizing 照常執行
    assert calls == {"pricing": 1, "sizing": 1}
    sj.pricing_and_sizing_scheduled()                       # 輸入沒變 → 不重建定價，但 sizing 仍跑（報價新鮮度隨時間變）
    assert calls == {"pricing": 1, "sizing": 2}
    with db.cursor() as cur:
        cur.execute("""INSERT INTO odds_snapshots (fetched_at, game_id, source, market, home_odds, away_odds)
                       VALUES (%s, %s, 'twsport', 'ml', 1.8, 2.0)""", (NOW - timedelta(minutes=2), gid))
    sj.pricing_and_sizing_scheduled()                       # 新盤口 → 重新定價
    assert calls == {"pricing": 2, "sizing": 3}
    with db.cursor() as cur:                                 # idle 也要有心跳（排程存活可見）
        cur.execute("SELECT source_key FROM data_sources WHERE source_key IN ('market_pricing', 'bet_sizing')")
        assert {r["source_key"] for r in cur.fetchall()} == {"market_pricing", "bet_sizing"}


def test_pricing_failure_is_retried_not_cached(db, seeded, monkeypatch):
    import core.pricing.job as pj
    import core.sizing.job as sj
    n = {"pricing": 0}

    class Rep:
        def __init__(self, errors):
            self.errors = errors

    def flaky(now=None, **k):
        n["pricing"] += 1
        return Rep([{"error": "artifact load failed"}] if n["pricing"] == 1 else [])
    monkeypatch.setattr(pj, "pricing_job", flaky)
    monkeypatch.setattr(sj, "sizing_job", lambda now=None, **k: None)
    monkeypatch.setattr(sj, "_LAST_PRICED_KEY", None)
    monkeypatch.setattr(sj, "now_utc", lambda: NOW)
    gid = add_game(db, seeded, NOW + 5 * H, nba="p2")
    with db.cursor() as cur:
        cur.execute("INSERT INTO odds_snapshots (fetched_at, game_id, source, market, home_odds, away_odds) "
                    "VALUES (%s, %s, 'twsport', 'ml', 1.9, 1.9)", (NOW - H, gid))
    sj.pricing_and_sizing_scheduled()
    sj.pricing_and_sizing_scheduled()
    sj.pricing_and_sizing_scheduled()
    assert n["pricing"] == 2          # 第一次失敗 → 第二次重試成功 → 第三次輸入未變而略過


def test_paper_scheduled_noop_vs_executes(db, seeded, monkeypatch):
    import core.execution.ledger as lg
    calls = {"decision": 0, "settle": 0}

    class R:
        strategies: dict = {}
        note = None
    monkeypatch.setattr(lg, "paper_decision_job", lambda now=None, **k: (calls.__setitem__("decision", calls["decision"] + 1), R())[1])
    monkeypatch.setattr(lg, "paper_settlement_job", lambda now=None, **k: calls.__setitem__("settle", calls["settle"] + 1))
    monkeypatch.setattr(lg, "now_utc", lambda: NOW)
    lg.paper_strategy_scheduled()                                       # 沒有候選、沒有未結算 → no-op
    assert calls == {"decision": 0, "settle": 0}
    # T-60 候選：開賽時間 = NOW + 30 分 → decision_time = NOW − 30 分 ≤ NOW（且在追蹤起點之後）
    add_game(db, seeded, NOW + timedelta(minutes=30), nba="t60")
    lg.paper_strategy_scheduled()
    assert calls == {"decision": 1, "settle": 1}
    with db.cursor() as cur:
        cur.execute("SELECT last_status FROM data_sources WHERE source_key = 'paper_strategy'")
        assert cur.fetchone()["last_status"] in ("ok", "warn")


def test_retrain_job_skips_without_new_data_and_runs_with_it(db, seeded, monkeypatch):
    from core.production import retrain as rt
    ran = []
    monkeypatch.setattr(rt, "retrain", lambda *a, **k: ran.append(1) or (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(rt, "now_utc", lambda: NOW)
    add_game(db, seeded, NOW - 2 * timedelta(days=1), nba="f1", status="final", pts=True)
    add_game(db, seeded, NOW - 3 * timedelta(days=1), nba="f2", status="final", pts=True)
    monkeypatch.setattr(activity, "current_artifact_n_games", lambda root=None: 2)
    assert rt.retrain_job() is None and ran == []                       # 沒有新資料 → 不訓練
    with db.cursor() as cur:
        cur.execute("SELECT last_status FROM data_sources WHERE source_key = 'model_retrain'")
        assert cur.fetchone()["last_status"] == "ok"                    # 不是錯誤
    add_game(db, seeded, NOW - timedelta(days=1), nba="f3", status="final", pts=True)
    assert rt.retrain_job() is None and ran == [1]                      # 有新資料 → 走既有 retrain 路徑（此處被 stub 丟錯）
    # 太新的比賽（< 4 小時緩衝）不算訓練資料
    monkeypatch.setattr(activity, "current_artifact_n_games", lambda root=None: 3)
    add_game(db, seeded, NOW - H, nba="f4", status="final", pts=True)
    ran.clear()
    assert rt.retrain_job() is None and ran == []


# ---- decision board ------------------------------------------------- #

def _user(db, email="eff@example.com"):
    with db.cursor() as cur:
        cur.execute("INSERT INTO users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,))
        return cur.fetchone()["id"]


def _snaps(db):
    with db.cursor() as cur:
        cur.execute("SELECT id, betting_day, input_fingerprint, last_confirmed_at FROM decision_snapshots ORDER BY id")
        return cur.fetchall()


@pytest.fixture()
def clean_idle_state(db):
    from core.decision import job as dj
    with db.cursor() as cur:      # conftest 的 TABLES 不含 users：其他測試留下的使用者會干擾狀態向量
        cur.execute("DELETE FROM users")
    dj.IDLE_STATE.update(key=None, at=None)
    yield dj
    dj.IDLE_STATE.update(key=None, at=None)


def test_decision_idle_confirms_without_rematerializing_and_keeps_freshness(db, seeded, clean_idle_state):
    dj = clean_idle_state
    _user(db)
    t0 = NOW
    rep = dj.decision_board_job(t0, allow_idle_skip=True)
    assert rep.idle is False and rep.snapshots_new >= 1                  # 第一次一定是完整物化
    base = _snaps(db)
    t1 = t0 + timedelta(minutes=1)
    rep = dj.decision_board_job(t1, allow_idle_skip=True)
    assert rep.idle is True and rep.snapshots_new == 0
    after = _snaps(db)
    assert [s["id"] for s in after] == [s["id"] for s in base]           # 沒有新增 / 重算
    assert all(s["last_confirmed_at"] == t1 for s in after)              # 但每分鐘仍被確認 → 10 分鐘 stale 門檻合約不變
    # idle 的內容等價：不用快路徑、同樣輸入在較晚的 now 重建，fingerprint 與已存的 snapshot 一致
    full = dj.decision_board_job(t1 + timedelta(minutes=5), dry_run=True)
    assert {b["fingerprint"] for b in full.boards} == {s["input_fingerprint"] for s in after}
    # 安全網：30 分鐘後一定完整物化一次
    rep = dj.decision_board_job(t0 + timedelta(minutes=31), allow_idle_skip=True)
    assert rep.idle is False and rep.snapshots_confirmed >= 1


def test_decision_never_skips_when_state_changes_or_work_exists(db, seeded, clean_idle_state):
    dj = clean_idle_state
    uid = _user(db)
    t = NOW
    dj.decision_board_job(t, allow_idle_skip=True)
    assert dj.decision_board_job(t + timedelta(minutes=1), allow_idle_skip=True).idle is True

    # 1) bankroll / risk_state 異動（使用者寫入 bets / ledger 都會推進 risk_state_version）→ 不得略過
    with db.cursor() as cur:
        cur.execute("INSERT INTO bankroll_accounts (user_id, currency, risk_state_version) VALUES (%s, 'TWD', 1) RETURNING id", (uid,))
        aid = cur.fetchone()["id"]
    assert dj.decision_board_job(t + timedelta(minutes=2), allow_idle_skip=True).idle is False
    assert dj.decision_board_job(t + timedelta(minutes=3), allow_idle_skip=True).idle is True
    with db.cursor() as cur:
        cur.execute("UPDATE bankroll_accounts SET risk_state_version = 2 WHERE id = %s", (aid,))
    assert dj.decision_board_job(t + timedelta(minutes=4), allow_idle_skip=True).idle is False

    # 2) 新使用者 → 不得略過
    dj.decision_board_job(t + timedelta(minutes=5), allow_idle_skip=True)
    assert dj.decision_board_job(t + timedelta(minutes=6), allow_idle_skip=True).idle is True
    _user(db, "second@example.com")
    assert dj.decision_board_job(t + timedelta(minutes=7), allow_idle_skip=True).idle is False

    # 3) 目標日附近出現未 final 的比賽 → 不 idle
    dj.decision_board_job(t + timedelta(minutes=8), allow_idle_skip=True)
    gid = add_game(db, seeded, t + 6 * H, nba="live-soon")
    with db.cursor() as cur:
        assert dj.idle_probe(cur, t + timedelta(minutes=9))[0] is False
    assert dj.decision_board_job(t + timedelta(minutes=9), allow_idle_skip=True).idle is False

    # 4) 未結算的實際注單 → 即使比賽已結束也不 idle（settle / ledger 同步必須照常跑）
    with db.cursor() as cur:
        cur.execute("UPDATE games SET status = 'final', home_pts = 100, away_pts = 99 WHERE id = %s", (gid,))
        cur.execute("""INSERT INTO bets (user_id, game_id, market, selection, odds, stake, result)
                       VALUES (%s, %s, 'ml', 'home', 1.9, 100, 'pending')""", (uid, gid))
        assert dj.idle_probe(cur, t + timedelta(minutes=10))[0] is False
    for k in range(3):
        assert dj.decision_board_job(t + timedelta(minutes=10 + k), allow_idle_skip=True).idle is False


def test_decision_cli_paths_never_use_idle_skip(db, seeded, clean_idle_state):
    dj = clean_idle_state
    _user(db)
    dj.decision_board_job(NOW, allow_idle_skip=True)
    assert dj.decision_board_job(NOW + timedelta(minutes=1)).idle is False       # 預設（CLI / 測試）一律完整路徑


# ---- readiness / production check ----------------------------------- #

def test_readiness_efficiency_summary_is_read_only_and_reports_states(db, seeded):
    from core.production import readiness as rd
    with db.cursor() as cur:
        out = rd.check_efficiency(cur, NOW)
        cur.execute("SELECT COUNT(*) AS n FROM data_sources")
        assert cur.fetchone()["n"] == 0                                           # 不寫任何東西
    by = {c.name: c for c in out}
    assert by["policy"].detail == "scheduler-efficiency-v1"
    assert by["next eligible game"].status == rd.WAITING and by["injury polling tier"].status == rd.WAITING
    add_game(db, seeded, NOW + 9 * H, nba="n1")
    with db.cursor() as cur:
        by = {c.name: c for c in rd.check_efficiency(cur, NOW)}
    assert "3-12h" in by["injury polling tier"].detail and "60" in by["injury polling tier"].detail
    assert by["pricing / sizing"].status == rd.PASS


def test_idle_injury_heartbeat_is_waiting_not_stale_warning(db, seeded):
    from core.production import readiness as rd
    with db.cursor() as cur:       # 一個很舊的 nbainjuries 心跳（離線期殘留）
        db.heartbeat(cur, source_key="nbainjuries", display_name="x", category="injury", status="ok", expected_interval_min=30)
        cur.execute("UPDATE data_sources SET last_attempt_at = %s, last_success_at = %s WHERE source_key = 'nbainjuries'",
                    (NOW - timedelta(days=60),) * 2)
        checks = {c.name: c for c in rd.check_heartbeats(cur, NOW, post_deploy=True)}
    assert checks["nbainjuries"].status == rd.WAITING                              # idle：沒有 36 小時內的比賽
    add_game(db, seeded, NOW + 2 * H, nba="soon")
    with db.cursor() as cur:
        checks = {c.name: c for c in rd.check_heartbeats(cur, NOW, post_deploy=True)}
    assert checks["nbainjuries"].status == rd.WARN                                 # 該抓卻很久沒抓 → 才警告
