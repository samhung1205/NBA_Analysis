"""來源 fallback 鏈、斷路器、來源心跳狀態、賽程同步（primary 失敗 → fallback）"""
from datetime import datetime, timedelta

import pytest
import requests

from core.sources import fallback as fb
from core.timeutil import UTC


def utc(*a):
    return datetime(*a, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def boom(msg="timeout"):
    def f():
        raise requests.ReadTimeout(msg)
    return f


# ---------------- 純邏輯：fallback 鏈 ---------------- #

def test_primary_success_no_fallback():
    r = fb.fetch_with_fallback("schedule", [("nba_cdn", lambda: [1, 2]), ("espn", boom())], fb.CircuitBreaker())
    assert (r.source, r.data, r.fallback_used) == ("nba_cdn", [1, 2], False)
    assert [a.status for a in r.attempts] == ["ok"]          # 備援根本不會被呼叫


def test_primary_failure_falls_back_and_is_traceable():
    r = fb.fetch_with_fallback("schedule", [("nba_cdn", boom("cdn 403")), ("nba_api", boom("stats timeout")),
                                            ("espn", lambda: ["g"])], fb.CircuitBreaker())
    assert r.source == "espn" and r.fallback_used and r.primary == "nba_cdn"
    assert [(a.source, a.status) for a in r.attempts] == [("nba_cdn", "error"), ("nba_api", "error"), ("espn", "ok")]
    assert "cdn 403" in r.attempts[0].error and "ReadTimeout" in r.attempts[1].error
    assert r.attempts[2].records == 1


def test_stats_timeout_does_not_abort_pipeline():
    # stats.nba.com 逾時屬預期失敗類型；錯誤被收進 attempts，不向外拋
    r = fb.fetch_with_fallback("schedule", [("nba_api", boom("Read timed out")), ("espn", lambda: [])],
                               fb.CircuitBreaker())
    assert r.source == "espn" and r.data == []               # 空清單 = 成功（今天可能沒比賽）


def test_empty_result_from_primary_is_success_not_failure():
    r = fb.fetch_with_fallback("schedule", [("nba_cdn", lambda: []), ("espn", lambda: ["x"])], fb.CircuitBreaker())
    assert r.source == "nba_cdn" and not r.fallback_used


def test_all_sources_failed_raises_with_attempts():
    with pytest.raises(fb.AllSourcesFailed) as ei:
        fb.fetch_with_fallback("schedule", [("nba_cdn", boom("a")), ("espn", boom("b"))], fb.CircuitBreaker())
    assert [a.status for a in ei.value.attempts] == ["error", "error"]
    assert "schedule" in str(ei.value)


def test_circuit_breaker_skips_after_consecutive_failures_and_recovers():
    clock, br = Clock(), None
    br = fb.CircuitBreaker(threshold=2, cooldown_s=600, clock=clock)
    calls = {"stats": 0}

    def stats():
        calls["stats"] += 1
        raise requests.ReadTimeout("t")

    chain = lambda: [("nba_api", stats), ("espn", lambda: ["ok"])]
    fb.fetch_with_fallback("s", chain(), br)
    fb.fetch_with_fallback("s", chain(), br)                 # 第 2 次失敗 → 開路
    r = fb.fetch_with_fallback("s", chain(), br)
    assert calls["stats"] == 2                               # 第 3 次被跳過，沒有再白等逾時
    assert r.attempts[0].status == "skipped" and r.source == "espn"

    clock.t = 601                                            # 冷卻結束 → 半開，再試一次
    fb.fetch_with_fallback("s", chain(), br)
    assert calls["stats"] == 3
    r = fb.fetch_with_fallback("s", chain(), br)             # 半開再失敗 → 立刻重新開路
    assert r.attempts[0].status == "skipped"


def test_circuit_breaker_success_resets_counter():
    br = fb.CircuitBreaker(threshold=2)
    br.record_failure("a")
    br.record_success("a")
    br.record_failure("a")
    assert not br.is_open("a")


# ---------------- DB：來源心跳狀態 ---------------- #

def _hb(db, key):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM data_sources WHERE source_key = %s", (key,))
        return cur.fetchone()


def A(source, status, error=None, records=None):
    return fb.Attempt(source, status, error, 0.1, records)


def test_heartbeat_ok_on_primary(db):
    fb.report_attempts("schedule", [A("nba_cdn", "ok", records=7)])
    r = _hb(db, "nba_cdn")
    assert r["last_status"] == "ok" and r["last_success_at"] and r["last_error"] is None and r["records_updated"] == 7


def test_heartbeat_fallback_marks_primary_warn_and_winner_ok_with_note(db):
    fb.report_attempts("schedule", [A("nba_cdn", "error", "ReadTimeout: x"), A("nba_api", "skipped", "斷路器"),
                                    A("espn", "ok", records=9)])
    cdn, stats, espn = _hb(db, "nba_cdn"), _hb(db, "nba_api"), _hb(db, "espn")
    assert cdn["last_status"] == "warn" and "ReadTimeout" in cdn["last_error"] and "espn" in cdn["last_error"]
    assert cdn["last_success_at"] is None
    assert stats is None                                     # skipped 沒有實際嘗試，不動心跳
    assert espn["last_status"] == "ok" and "備援 nba_cdn" in espn["last_error"] and espn["last_success_at"]


def test_heartbeat_all_failed_is_error(db):
    fb.report_attempts("schedule", [A("nba_cdn", "error", "a"), A("espn", "error", "b")])
    assert _hb(db, "nba_cdn")["last_status"] == "error" and _hb(db, "espn")["last_status"] == "error"


def test_heartbeat_optional_source_failure_is_only_warn(db):
    fb.report_attempts("box_advanced", [A("nba_api", "error", "timeout")], optional=True)
    assert _hb(db, "nba_api")["last_status"] == "warn"


def test_heartbeat_recovery_clears_error_and_keeps_last_success(db):
    fb.report_attempts("schedule", [A("nba_cdn", "ok")])
    first = _hb(db, "nba_cdn")["last_success_at"]
    fb.report_attempts("schedule", [A("nba_cdn", "error", "boom"), A("espn", "ok")])
    mid = _hb(db, "nba_cdn")
    assert mid["last_status"] == "warn" and mid["last_success_at"] == first   # 失敗不覆蓋上次成功時間
    fb.report_attempts("schedule", [A("nba_cdn", "ok")])
    after = _hb(db, "nba_cdn")
    assert after["last_status"] == "ok" and after["last_error"] is None and after["last_success_at"] >= first


def test_heartbeat_is_one_row_per_source(db):
    for _ in range(3):
        fb.report_attempts("schedule", [A("nba_cdn", "ok")])
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM data_sources WHERE source_key='nba_cdn'")
        assert cur.fetchone()["n"] == 1


def test_fallback_sources_get_relaxed_staleness_threshold(db):
    fb.report_attempts("schedule", [A("espn", "ok")])
    assert _hb(db, "espn")["expected_interval_min"] >= 60 * 24 * 7     # 備援平常不跑，不該被標 stale


# ---------------- DB：賽程同步 primary → fallback ---------------- #

def game(gid, home="BOS", away="NYK", when=utc(2026, 10, 24, 23), status="scheduled", source="nba_cdn", **kw):
    ids = {"BOS": 1610612738, "NYK": 1610612752, "LAL": 1610612747}
    g = {"nba_game_id": gid, "source": source, "season": "2026-27", "season_stage": "regular",
         "date_utc": when, "status": status, "home_nba_team_id": ids[home] if source != "espn" else None,
         "away_nba_team_id": ids[away] if source != "espn" else None, "home_abbr": home, "away_abbr": away,
         "home_pts": None, "away_pts": None}
    g.update(kw)
    return g


@pytest.fixture()
def sources(monkeypatch):
    """可程式化的來源替身：.cdn / .stats / .espn 設成 list（成功）或 Exception（失敗）。"""
    from core.jobs import games_sync
    from core.sources.fallback import CircuitBreaker
    import core.sources.fallback as fbmod

    class S:
        cdn, stats, espn = [], [], []
        box_cdn = box_stats = box_adv = None
        calls = {"cdn": 0, "stats": 0, "espn": 0}

    def pick(name):
        def f(*a, **k):
            S.calls[name] += 1
            v = getattr(S, name)
            if isinstance(v, Exception):
                raise v
            return v
        return f

    monkeypatch.setattr(games_sync.nba_cdn, "fetch_games_in_window", pick("cdn"))
    monkeypatch.setattr(games_sync.nba_stats, "fetch_games_for_et_dates", pick("stats"))
    monkeypatch.setattr(games_sync.espn, "fetch_games_for_et_dates", pick("espn"))
    monkeypatch.setattr(fbmod, "default_breaker", CircuitBreaker())
    return S


W = (utc(2026, 10, 24), utc(2026, 10, 26))


def _games(db):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM games ORDER BY id")
        return cur.fetchall()


def test_sync_primary_cdn_ok(seeded, db, sources):
    from core.jobs.games_sync import sync_games

    sources.cdn = [game("0022600001")]
    rep = sync_games(*W)
    assert (rep.source, rep.fallback_used, rep.games_upserted) == ("nba_cdn", False, 1)
    assert sources.calls == {"cdn": 1, "stats": 0, "espn": 0}
    assert _hb(db, "nba_cdn")["last_status"] == "ok"


def test_sync_cdn_fails_stats_times_out_then_espn_fallback(seeded, db, sources):
    from core.jobs.games_sync import sync_games

    sources.cdn = requests.HTTPError("403")
    sources.stats = requests.ReadTimeout("stats.nba.com timed out")
    sources.espn = [game("espn:401", source="espn")]
    rep = sync_games(*W)                                      # 不得丟出例外
    assert rep.source == "espn" and rep.fallback_used and rep.games_upserted == 1 and rep.error is None
    assert [g["nba_game_id"] for g in _games(db)] == ["espn:401"]
    assert _hb(db, "nba_cdn")["last_status"] == "warn"
    assert _hb(db, "nba_api")["last_status"] == "warn"
    assert "ReadTimeout" in _hb(db, "nba_api")["last_error"]
    assert _hb(db, "espn")["last_status"] == "ok" and "備援 nba_cdn" in _hb(db, "espn")["last_error"]


def test_sync_all_sources_fail_returns_error_report_without_raising(seeded, db, sources):
    from core.jobs.games_sync import sync_games

    sources.cdn = sources.stats = sources.espn = requests.ConnectionError("down")
    rep = sync_games(*W)
    assert rep.error and rep.games_upserted == 0
    assert all(_hb(db, k)["last_status"] == "error" for k in ("nba_cdn", "nba_api", "espn"))


def test_espn_fallback_does_not_duplicate_game_official_source_already_wrote(seeded, db, sources):
    from core.jobs.games_sync import sync_games

    sources.cdn = [game("0022600001")]
    sync_games(*W)
    sources.cdn = requests.HTTPError("403")
    sources.stats = requests.ReadTimeout("t")
    sources.espn = [game("espn:401", source="espn", when=utc(2026, 10, 24, 23, 30))]   # 開賽時間略有差異
    sync_games(*W)
    assert [g["nba_game_id"] for g in _games(db)] == ["0022600001"]


def test_official_id_adopts_earlier_espn_row(seeded, db, sources):
    from core.jobs.games_sync import sync_games

    sources.cdn = requests.HTTPError("403")
    sources.stats = requests.ReadTimeout("t")
    sources.espn = [game("espn:401", source="espn")]
    sync_games(*W)
    sources.cdn = [game("0022600001")]
    sync_games(*W)
    rows = _games(db)
    assert [g["nba_game_id"] for g in rows] == ["0022600001"]


def test_final_game_is_not_downgraded_by_stale_schedule(seeded, db, sources):
    from core.jobs.games_sync import sync_games

    q = {"home_q1": 30, "home_q2": 30, "home_q3": 30, "home_q4": 30, "away_q1": 20, "away_q2": 20,
         "away_q3": 20, "away_q4": 20}
    sources.cdn = [game("0022600001", status="final", home_pts=120, away_pts=80, **q)]
    sync_games(*W)
    sources.cdn = [game("0022600001", status="live", home_pts=60, away_pts=40)]      # 過期快取
    sync_games(*W)
    g = _games(db)[0]
    assert g["status"] == "final" and g["home_pts"] == 120


def test_fallback_chain_filters_by_game_time_utc_not_by_et_date(sources):
    """stats / espn 以 ET 日期查詢，可能多回視窗外的比賽；視窗過濾以 game_time_utc 為準。"""
    from core.jobs import games_sync

    sources.stats = [game("0022600001", source="nba_api", when=utc(2026, 10, 25, 2, 30)),   # ET 10/24 22:30 → 視窗內
                     game("0022600002", source="nba_api", when=utc(2026, 10, 27, 1))]        # 視窗外
    sources.espn = [game("espn:1", source="espn", when=utc(2026, 10, 23, 23)),                # 視窗外
                    game("espn:2", source="espn", when=utc(2026, 10, 25, 2, 30))]
    chain = dict(games_sync._schedule_chain(*W))
    assert [g["nba_game_id"] for g in chain["nba_api"]()] == ["0022600001"]
    assert [g["nba_game_id"] for g in chain["espn"]()] == ["espn:2"]


# ---------------- DB：box score 的 fallback ---------------- #

def _final_game_in_db(db, seeded):
    with db.cursor() as cur:
        return db.upsert_game(cur, nba_game_id="0022600001", season="2026-27", season_stage="regular",
                              date_utc=utc(2026, 10, 24, 23), home_team_id=seeded["BOS"],
                              away_team_id=seeded["NYK"], status="final", home_pts=100, away_pts=90)


def _box():
    side = lambda pid, name: {"team": {"pts": 100, "reb": 40, "oreb": 10, "dreb": 30, "ast": 20, "tov": 12,
                                       "fgm": 38, "fga": 85, "fg3m": 10, "fg3a": 30, "ftm": 14, "fta": 18,
                                       "team_min": 240.0}, "players": [
        {"nba_player_id": pid, "name": name, "position": "SF", "started": 1, "min": 30.0, "pts": 20,
         "reb": 5, "ast": 5, "stl": 1, "blk": 0, "tov": 2, "fgm": 8, "fga": 15, "fg3m": 2, "fg3a": 5,
         "ftm": 2, "fta": 2, "plus_minus": 5.0}]}
    box = {"home": side(1627759, "Jaylen Brown"), "away": side(1629020, "Jaren Jackson Jr.")}
    box["quarters"] = {"home_q1": 25, "home_q2": 25, "home_q3": 25, "home_q4": 25, "away_q1": 20,
                       "away_q2": 20, "away_q3": 25, "away_q4": 25, "home_h1": 50, "home_h2": 50,
                       "away_h1": 40, "away_h2": 50, "home_ot": None, "away_ot": None}
    return box


def test_box_cdn_fails_falls_back_to_stats_and_advanced_failure_is_non_fatal(seeded, db, monkeypatch):
    from core.jobs import games_sync
    from core.sources.fallback import CircuitBreaker
    import core.sources.fallback as fbmod

    monkeypatch.setattr(fbmod, "default_breaker", CircuitBreaker())
    gid = _final_game_in_db(db, seeded)
    monkeypatch.setattr(games_sync.nba_cdn, "fetch_box_basic", lambda g: (_ for _ in ()).throw(requests.HTTPError("403")))
    monkeypatch.setattr(games_sync.nba_stats, "fetch_box_traditional", lambda g: _box())
    monkeypatch.setattr(games_sync.nba_stats, "fetch_box_advanced",
                        lambda g: (_ for _ in ()).throw(requests.ReadTimeout("stats timeout")))
    rep = games_sync.SyncReport()
    games_sync._sync_box_scores(utc(2026, 10, 24), utc(2026, 10, 26), rep)
    assert rep.box_basic_done == 1 and rep.box_advanced_done == 0
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM team_game_stats WHERE game_id=%s", (gid,))
        assert cur.fetchone()["n"] == 2
        cur.execute("SELECT home_q1, home_h1 FROM games WHERE id=%s", (gid,))
        assert cur.fetchone() == {"home_q1": 25, "home_h1": 50}
    assert _hb(db, "nba_api")["last_status"] == "warn"          # 進階數據缺漏：降級警示，不是 error


def test_box_retries_advanced_next_run_then_stops(seeded, db, monkeypatch):
    from core.jobs import games_sync
    from core.sources.fallback import CircuitBreaker
    import core.sources.fallback as fbmod

    monkeypatch.setattr(fbmod, "default_breaker", CircuitBreaker())
    _final_game_in_db(db, seeded)
    monkeypatch.setattr(games_sync.nba_cdn, "fetch_box_basic", lambda g: _box())
    adv = {"pace": 98.0, "off_rtg": 110.0, "def_rtg": 105.0, "net_rtg": 5.0, "ts_pct": 0.58, "efg_pct": 0.54,
           "ast_ratio": 17.0, "tov_ratio": 12.0}
    state = {"fail": True, "adv_calls": 0}

    def fetch_adv(g):
        state["adv_calls"] += 1
        if state["fail"]:
            raise requests.ReadTimeout("t")
        return {"home": adv, "away": adv}

    monkeypatch.setattr(games_sync.nba_stats, "fetch_box_advanced", fetch_adv)
    games_sync._sync_box_scores(utc(2026, 10, 24), utc(2026, 10, 26), games_sync.SyncReport())
    state["fail"] = False
    rep = games_sync.SyncReport()
    games_sync._sync_box_scores(utc(2026, 10, 24), utc(2026, 10, 26), rep)
    assert rep.box_basic_done == 0 and rep.box_advanced_done == 1     # 基本數據已齊，只補進階
    rep = games_sync.SyncReport()
    games_sync._sync_box_scores(utc(2026, 10, 24), utc(2026, 10, 26), rep)
    assert rep.box_advanced_done == 0 and state["adv_calls"] == 2       # 補齊後不再重複抓
