"""D.1 盤口快照儲存與抓取工作（DB：暫存 schema）
去重 / 線動 / 只動價 / 不覆蓋歷史 / 時間順序 / 併發冪等 / 端到端 job / 來源互相隔離 / 心跳狀態"""
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.odds import ingest
from core.odds.canonical import FULL_GAME, MONEYLINE, OPEN, SPREAD, SUSPENDED, make_snapshot
from core.odds.errors import Blocked
from core.odds.store import persist_snapshots
from tests.helpers import count

FX = Path(__file__).parent / "fixtures" / "odds"
UTC = timezone.utc
T0 = datetime(2026, 10, 4, 4, 0, tzinfo=UTC)
TIP = datetime(2026, 10, 4, 23, 0, tzinfo=UTC)


def spread(line=-5.5, home=1.9, away=1.9, status=OPEN, src_time=None, source="twsport", bookmaker="twsport"):
    return make_snapshot(source=source, bookmaker=bookmaker, source_event_id="E1", market_type=SPREAD,
                         period=FULL_GAME, status=status, prices={"home": home, "away": away}, home_line=line,
                         source_updated_at=src_time, raw={"t": "x"})


@pytest.fixture()
def game(db, seeded):
    with db.cursor() as cur:
        return db.upsert_game(cur, nba_game_id="0022600001", season="2026-27", season_stage="regular",
                              date_utc=TIP, home_team_id=seeded["BOS"], away_team_id=seeded["NYK"], status="scheduled")


def write(db, gid, snap, at, source="twsport"):
    with db.transaction() as cur:
        return persist_snapshots(cur, [(gid, snap)], source=source, fetched_at=at)


def rows(db, gid):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM odds_snapshots WHERE game_id = %s ORDER BY fetched_at, id", (gid,))
        return cur.fetchall()


# ---- snapshot semantics ------------------------------------------------------ #

def test_identical_poll_does_not_duplicate(db, game):
    assert write(db, game, spread(), T0).new == 1
    st = write(db, game, spread(), T0 + timedelta(minutes=30))
    assert (st.new, st.unchanged) == (0, 1)
    r = rows(db, game)
    assert len(r) == 1 and r[0]["fetched_at"] == T0 and r[0]["last_seen_at"] == T0 + timedelta(minutes=30)
    assert write(db, game, spread(), T0).unchanged == 1            # 同一次抓取重跑：冪等
    assert rows(db, game)[0]["last_seen_at"] == T0 + timedelta(minutes=30)   # last_seen_at 不倒退


def test_line_move_and_price_only_move_create_new_snapshots_and_never_overwrite(db, game):
    write(db, game, spread(-5.5), T0)
    first = rows(db, game)[0]
    assert write(db, game, spread(-6.5), T0 + timedelta(minutes=30)).new == 1             # 線動
    assert write(db, game, spread(-6.5, 1.85, 1.95), T0 + timedelta(minutes=60)).new == 1  # 只動價
    assert write(db, game, spread(-5.5), T0 + timedelta(minutes=90)).new == 1              # A→B→A 也要新列
    r = rows(db, game)
    assert [x["line"] for x in r] == [-5.5, -6.5, -6.5, -5.5]
    assert [(x["home_odds"], x["away_odds"]) for x in r][2] == (1.85, 1.95)
    old = r[0]
    for k in ("fetched_at", "line", "home_odds", "away_odds", "content_hash", "model_threshold", "raw_json"):
        assert old[k] == first[k]                                                           # 舊列內容從未被改
    assert old["model_threshold"] == 5.5 and old["away_line"] == 5.5 and old["market"] == "spread"
    assert old["bookmaker"] == "twsport" and old["model_target"] == "margin" and old["market_status"] == "open"


def test_suspension_is_a_new_snapshot(db, game):
    write(db, game, spread(), T0)
    assert write(db, game, spread(status=SUSPENDED), T0 + timedelta(minutes=30)).new == 1
    assert [x["market_status"] for x in rows(db, game)] == ["open", "suspended"]


def test_out_of_order_and_time_semantics(db, game):
    write(db, game, spread(-5.5), T0 + timedelta(hours=1))
    st = write(db, game, spread(-7.5), T0)                       # 較舊的觀測晚到 → 不可插進歷史
    assert (st.new, st.stale) == (0, 1)
    write(db, game, spread(-5.5), T0 + timedelta(hours=2))       # last_seen 推到 +2h
    st = write(db, game, spread(-8.5), T0 + timedelta(minutes=90))   # 介於 fetched_at 與 last_seen 之間的不同內容
    assert st.stale == 1 and len(rows(db, game)) == 1
    # source_updated_at 與 fetched_at 分開；來源時間在未來（超過容許誤差）→ 欄位留空、原值進 raw
    write(db, game, spread(-6.5, src_time=T0 + timedelta(hours=1, minutes=50)), T0 + timedelta(hours=3))
    write(db, game, spread(-4.5, src_time=T0 + timedelta(hours=9)), T0 + timedelta(hours=4))
    r = rows(db, game)
    assert r[1]["source_updated_at"] == T0 + timedelta(hours=1, minutes=50) and r[1]["fetched_at"] == T0 + timedelta(hours=3)
    assert r[2]["source_updated_at"] is None and "source_updated_at_future" in r[2]["raw_json"]


def test_bookmakers_are_separate_series(db, game):
    write(db, game, spread(source="oddsapi", bookmaker="draftkings"), T0, source="oddsapi")
    st = write(db, game, spread(source="oddsapi", bookmaker="fanduel"), T0, source="oddsapi")
    assert st.new == 1 and count(db, "odds_snapshots") == 2


def test_concurrent_writers_are_idempotent(db, game):
    write(db, game, spread(-5.5), T0)
    barrier = threading.Barrier(4)
    errors = []

    def worker(i):
        try:
            barrier.wait()
            write(db, game, spread(-6.5), T0 + timedelta(minutes=30, seconds=i))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert [x["line"] for x in rows(db, game)] == [-5.5, -6.5]      # 同一份新內容只會一列


def test_canonical_quote_view(db, game):
    write(db, game, spread(-5.5, 1.8, 2.0), T0)
    with db.cursor() as cur:
        cur.execute("SELECT side, price, display_line, model_threshold, comparator FROM v_odds_quotes ORDER BY side")
        q = {r["side"]: r for r in cur.fetchall()}
    assert set(q) == {"home", "away"}
    assert (q["home"]["display_line"], q["home"]["model_threshold"], q["home"]["comparator"]) == (-5.5, 5.5, "gt")
    assert (q["away"]["display_line"], q["away"]["model_threshold"], q["away"]["comparator"]) == (5.5, 5.5, "lt")


# ---- end-to-end job ------------------------------------------------------------ #

def _fixture_raw(stage="regular"):
    raw = json.loads((FX / "twsport_raw_preseason_20261004.json").read_text(encoding="utf-8"))
    for t in raw["tournaments"]:
        if t["numevents"]:
            t["stage"] = stage
    return raw


@pytest.fixture()
def tsl_games(db, seeded):
    with db.cursor() as cur:
        ids = dict(seeded)
        for nba_id, abbr, name in [(1610612746, "LAC", "Los Angeles Clippers"), (1610612744, "GSW", "Golden State Warriors"),
                                    (1610612762, "UTA", "Utah Jazz"), (1610612743, "DEN", "Denver Nuggets")]:
            ids[abbr] = db.upsert_team(cur, nba_team_id=nba_id, abbr=abbr, name=name)
        g1 = db.upsert_game(cur, nba_game_id="0022600011", season="2026-27", season_stage="regular", date_utc=TIP,
                            home_team_id=ids["LAC"], away_team_id=ids["GSW"], status="scheduled")
        g2 = db.upsert_game(cur, nba_game_id="0022600012", season="2026-27", season_stage="regular", date_utc=TIP,
                            home_team_id=ids["DEN"], away_team_id=ids["UTA"], status="scheduled")
    return {"LAC_GSW": g1, "DEN_UTA": g2, **ids}


def _status(db, key):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM data_sources WHERE source_key = %s", (key,))
        return cur.fetchone()


def test_job_end_to_end_success_then_idempotent_rerun(db, tsl_games):
    raw = _fixture_raw()
    rep = ingest.run_odds_job("twsport", fetch=lambda r: raw, fetched_at=T0, db=db)
    assert rep.outcome == "success" and rep.n_matched == 2 and rep.n_snapshots_new == 6
    assert rep.market_counts == {"moneyline": 2, "spread": 2, "total": 2}
    with db.cursor() as cur:
        cur.execute("SELECT game_id, status, method FROM odds_event_links ORDER BY source_event_id")
        links = cur.fetchall()
        cur.execute("SELECT outcome, n_snapshots_new, fetched_at FROM odds_fetch_runs")
        run = cur.fetchone()
    assert {l["game_id"] for l in links} == {tsl_games["LAC_GSW"], tsl_games["DEN_UTA"]}
    assert all(l["status"] == "matched" and l["method"] == "teams_time" for l in links)
    assert run["outcome"] == "success" and run["n_snapshots_new"] == 6 and run["fetched_at"] == T0
    hb = _status(db, "twsport")
    assert (hb["last_status"], hb["last_outcome"], hb["category"]) == ("ok", "success", "odds")

    rep2 = ingest.run_odds_job("twsport", fetch=lambda r: raw, fetched_at=T0 + timedelta(minutes=30), db=db)
    assert (rep2.n_snapshots_new, rep2.n_snapshots_unchanged) == (0, 6) and count(db, "odds_snapshots") == 6
    sel = raw["tournaments"][2]["event_groups"][0]["data"]["events"][0]["markets"][0]["selections"][0]
    sel["currentpriceup"] = "14"                                                   # 1.52 → 1.56
    rep3 = ingest.run_odds_job("twsport", fetch=lambda r: raw, fetched_at=T0 + timedelta(minutes=60), db=db)
    assert (rep3.n_snapshots_new, rep3.n_snapshots_unchanged) == (1, 5) and count(db, "odds_snapshots") == 7


def test_dry_run_writes_nothing(db, tsl_games):
    rep = ingest.run_odds_job("twsport", fetch=lambda r: _fixture_raw(), fetched_at=T0, dry_run=True, db=db)
    assert rep.outcome == "success" and rep.n_matched == 2 and rep.diagnostics["would_write"] == 6
    for t in ("odds_snapshots", "odds_event_links", "odds_fetch_runs", "data_sources"):
        assert count(db, t) == 0


def test_partial_when_an_event_cannot_be_matched(db, tsl_games):
    raw = _fixture_raw()
    raw["tournaments"][2]["event_groups"][0]["data"]["events"][1]["participantname_home"] = "丹佛掘金者"
    rep = ingest.run_odds_job("twsport", fetch=lambda r: raw, fetched_at=T0, db=db)
    assert rep.outcome == "partial" and rep.n_matched == 1 and rep.n_unmatched == 1 and rep.n_snapshots_new == 3
    assert _status(db, "twsport")["last_status"] == "warn"
    with db.cursor() as cur:
        cur.execute("SELECT status, reason, game_id FROM odds_event_links WHERE status <> 'matched'")
        assert cur.fetchone() == {"status": "unmatched", "reason": "unknown_team", "game_id": None}


def test_preseason_events_are_expected_unmatched_not_alerts(db, tsl_games):
    with db.cursor() as cur:
        cur.execute("DELETE FROM games")
    rep = ingest.run_odds_job("twsport", fetch=lambda r: _fixture_raw("preseason"), fetched_at=T0, db=db)
    assert rep.outcome == "success" and rep.n_unmatched == 2 and rep.n_snapshots_new == 0
    assert {e["reason"] for e in rep.events} == {"preseason_not_tracked"}


def test_in_play_quotes_are_not_written(db, tsl_games):
    rep = ingest.run_odds_job("twsport", fetch=lambda r: _fixture_raw(), fetched_at=TIP + timedelta(minutes=5), db=db)
    assert rep.n_matched == 2 and rep.n_snapshots_new == 0 and all(e["skipped_in_play"] for e in rep.events)


def test_no_markets_vs_parser_changed_are_distinguishable(db, tsl_games, monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "RAW_SAMPLE_DIR", tmp_path)
    empty = _fixture_raw()
    for t in empty["tournaments"]:
        t["numevents"], t["event_groups"] = 0, []
    rep = ingest.run_odds_job("twsport", fetch=lambda r: empty, fetched_at=T0, db=db)
    hb = _status(db, "twsport")
    assert rep.outcome == "no_nba_markets" and (hb["last_status"], hb["last_outcome"]) == ("ok", "no_nba_markets")

    broken = _fixture_raw()
    for ev in broken["tournaments"][2]["event_groups"][0]["data"]["events"]:
        for mk in ev["markets"]:
            mk["externalreference"] = "X" + mk["externalreference"]
    rep2 = ingest.run_odds_job("twsport", fetch=lambda r: broken, fetched_at=T0, db=db)
    hb2 = _status(db, "twsport")
    assert rep2.outcome == "parser_changed" and (hb2["last_status"], hb2["last_outcome"]) == ("error", "parser_changed")
    assert hb2["last_success_at"] == hb["last_success_at"]                # 失敗不更新 last_success_at
    assert rep2.diagnostics.get("raw_sample", "").startswith(str(tmp_path))   # 保存 raw 樣本供追查

    gone = _fixture_raw()
    gone["tournaments"][2]["event_groups"] = []                          # 宣稱 2 場但沒有賽事
    assert ingest.run_odds_job("twsport", fetch=lambda r: gone, fetched_at=T0, db=db).outcome == "parser_changed"


def test_one_source_failing_does_not_break_another(db, tsl_games):
    def blocked(rep):
        raise Blocked("Cloudflare challenge 未自動通過")
    rep = ingest.run_odds_job("twsport", fetch=blocked, db=db)
    assert rep.outcome == "blocked" and _status(db, "twsport")["last_status"] == "error"

    oa = json.loads((FX / "oddsapi_raw_synthetic.json").read_text())
    rep2 = ingest.run_odds_job("oddsapi", fetch=lambda r: oa, fetched_at=T0, db=db)
    assert rep2.outcome in ("success", "partial") and _status(db, "oddsapi")["last_outcome"] == rep2.outcome
    assert _status(db, "twsport")["last_outcome"] == "blocked"


def test_blocked_backoff_skips_browser(db, tsl_games, monkeypatch):
    for i in range(3):
        ingest.run_odds_job("twsport", fetch=lambda r: (_ for _ in ()).throw(Blocked("x")), db=db)
    monkeypatch.setattr(ingest, "_fetch_twsport", lambda rep: pytest.fail("退避期間不應開瀏覽器"))
    rep = ingest.run_odds_job("twsport", db=db)
    assert rep.outcome == "blocked" and rep.skipped and count(db, "odds_fetch_runs") == 3


def test_unexpected_exception_is_contained(db, tsl_games):
    rep = ingest.run_odds_job("twsport", fetch=lambda r: 1 / 0, db=db)
    assert rep.outcome == "error" and "ZeroDivisionError" in rep.error
    with db.cursor() as cur:
        cur.execute("SELECT outcome, finished_at FROM odds_fetch_runs")
        r = cur.fetchone()
    assert r["outcome"] == "error" and r["finished_at"] is not None


def test_oddsapi_not_configured_is_reported(db, monkeypatch):
    import dataclasses
    monkeypatch.setattr(ingest, "settings", dataclasses.replace(ingest.settings, odds_api_key=None))
    rep = ingest.run_odds_job("oddsapi", db=db)
    assert rep.outcome == "not_configured" and _status(db, "oddsapi")["last_status"] == "warn"
