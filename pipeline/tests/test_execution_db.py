"""Phase D.4：paper ledger / prospective job / historical backtest（暫存 schema；真實合成 artifact → D.2 → D.3 → execution-v1）

- decision 冪等、永不重做；no_bet 也保存；job 晚執行仍只用 T 以前的資料；晚太多 → decision_window_missed
- 結算後不可改（trigger）；decision 不可改 / 刪；不寫 bets
- day_start 凍結、次日使用已結算 bankroll
- prospective decision = 之後以歷史引擎重建同一 T 的結果
- 歷史引擎：沒有 observed 盤口 → historical_evidence_available = false；seed 列被拒；Odds API 標為國際盤診斷
"""
from __future__ import annotations

import shutil
from datetime import date, timedelta

import psycopg
import pytest

from core.execution import asof, ledger
from core.execution.backtest import run_strategy_backtest
from core.execution.policy import EXECUTION_V1, PRIMARY_SCOPE, parse_scope, strategy_id
from core.odds import store
from core.odds.canonical import FULL_GAME, MONEYLINE, OPEN, make_snapshot
from core.odds.store import persist_snapshots
from core.pricing import engine as pricing_engine
from core.sizing.policy import RISK_V1
from test_pricing_artifact import S, TIP, game, insert_pred, league, setup  # noqa: F401
from test_sizing_db import generous_ml

T = TIP - timedelta(minutes=60)
SID = strategy_id(EXECUTION_V1, RISK_V1, PRIMARY_SCOPE)
M = timedelta(minutes=1)


def write_obs(db, gid, snap, at, *, covered=True):
    """模擬一次真實輪詢：odds_fetch_runs（success）+ D.1 persist_snapshots（fetch_run_id）。"""
    with db.cursor() as cur:
        rid = store.start_run(cur, snap.source, at)
    with db.transaction() as cur:
        persist_snapshots(cur, [(gid, snap)], source=snap.source, fetched_at=at, run_id=rid)
    events = [{"game_id": gid, "status": "matched", "n_snapshots": 1}] if covered else []
    with db.cursor() as cur:
        store.finish_run(cur, rid, fetched_at=at, finished_at=at, outcome="success", diagnostics={"events": events})
    return rid


def rows(db, table, where="", params=()):
    with db.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} {where} ORDER BY id" if table != "paper_strategy_days" else
                    f"SELECT * FROM {table} {where} ORDER BY betting_day", params)
        return cur.fetchall()


def second_game(db, seeded, tip, nba_id="0022600100"):
    with db.cursor() as cur:
        return db.upsert_game(cur, nba_game_id=nba_id, season="2026-27", season_stage="regular", date_utc=tip,
                              home_team_id=seeded["BOS"], away_team_id=seeded["LAL"], status="scheduled")


def finalize(db, gid, home=(30, 28, 27, 25), away=(25, 25, 25, 25)):
    with db.cursor() as cur:
        cur.execute("""UPDATE games SET status = 'final', home_pts = %s, away_pts = %s, home_h1 = %s, away_h1 = %s,
                         home_q1 = %s, home_q2 = %s, home_q3 = %s, home_q4 = %s,
                         away_q1 = %s, away_q2 = %s, away_q3 = %s, away_q4 = %s WHERE id = %s""",
                    (sum(home), sum(away), home[0] + home[1], away[0] + away[1], *home, *away, gid))


def prepare_bet(db, gid, setup, *, at=T - 20 * M):
    pid = insert_pred(db, gid, setup, T - 30 * M, kind="final")
    oh, oa = generous_ml(setup)
    write_obs(db, gid, S(MONEYLINE, prices={"home": oh, "away": oa}), at)
    return pid, oh


def job(db, setup, now, **kw):
    return ledger.paper_decision_job(now, root=setup["root"], db=db, **kw)


# ======================= decisions ======================= #

def test_paper_decisions_persisted_idempotent_and_never_replaced(db, game, setup, seeded):
    pid, oh = prepare_bet(db, game, setup)
    g2 = second_game(db, seeded, TIP)                                                 # 同一 T、沒有盤口
    insert_pred(db, g2, setup, T - 30 * M, kind="final")
    rep = job(db, setup, T + 2 * M)
    assert rep.strategies[SID]["decided"] == 2
    ds = rows(db, "paper_strategy_decisions")
    by = {d["game_id"]: d for d in ds}
    d = by[game]
    assert d["decision_status"] == "bet" and d["no_bet_reason"] is None and d["prediction_id"] == pid
    assert d["decision_time"] == T and d["evaluated_at"] == T + 2 * M and d["evaluation_lag_seconds"] == 120
    assert (d["execution_policy_version"], d["risk_policy_version"], d["sizing_version"], d["pricing_version"],
            d["kelly_math_version"]) == ("execution-v1", "risk-v1", "sizing-v1", "pricing-v1", "kelly-push-v1")
    assert (d["strategy_scope"], d["evidence_label"], d["source"], d["bookmaker"]) == (
        "primary", "taiwan_sports_lottery_strategy", "twsport", "twsport")
    assert d["artifact_version"] == setup["art_a"].artifact_version and d["pricing_fingerprint"]
    assert (by[g2]["decision_status"], by[g2]["no_bet_reason"]) == ("no_bet", "no_odds")   # no-bet 也保存
    bets = rows(db, "paper_strategy_bets")
    assert len(bets) == 1 and bets[0]["side"] == "home" and bets[0]["decimal_odds"] == oh
    assert 0 < bets[0]["stake_fraction"] <= 0.02 and bets[0]["settlement_status"] == "pending"
    assert bets[0]["stake_units"] == pytest.approx(d["day_start_bankroll"] * bets[0]["stake_fraction"], rel=1e-12)
    # 之後出現更好的賠率、更新的預測 → 重跑不新增、不改寫
    write_obs(db, game, S(MONEYLINE, prices={"home": round(oh + 0.5, 2), "away": 1.3}), T + 5 * M)
    insert_pred(db, game, setup, T + 6 * M, kind="refresh")
    rep2 = job(db, setup, T + 7 * M)
    assert rep2.strategies[SID]["decided"] == 0
    assert [dict(r) for r in rows(db, "paper_strategy_decisions")] == [dict(r) for r in ds]
    assert [dict(r) for r in rows(db, "paper_strategy_bets")] == [dict(r) for r in bets]
    assert rows(db, "bets") == []                                                    # 不碰使用者的 bets


def test_late_job_uses_only_data_at_exact_t(db, game, setup, seeded):
    """job 在 T+3 分執行：T+1 分才出現的盤口 / 預測一律不用 → no_bet，不回填。"""
    insert_pred(db, game, setup, T - 30 * M, kind="final")
    oh, oa = generous_ml(setup)
    write_obs(db, game, S(MONEYLINE, prices={"home": oh, "away": oa}), T + 1 * M)
    g2 = second_game(db, seeded, TIP)
    write_obs(db, g2, S(MONEYLINE, prices={"home": oh, "away": oa}), T - 20 * M)
    insert_pred(db, g2, setup, T + 1 * M, kind="final")
    job(db, setup, T + 3 * M)
    by = {d["game_id"]: d for d in rows(db, "paper_strategy_decisions")}
    assert by[game]["no_bet_reason"] == "no_odds" and by[game]["odds_snapshot_ids"] == []
    assert by[g2]["no_bet_reason"] == "no_prediction" and by[g2]["prediction_id"] is None
    assert all(d["decision_time"] == T for d in by.values()) and rows(db, "paper_strategy_bets") == []


def test_missed_decision_window_recorded_not_backfilled(db, game, setup):
    prepare_bet(db, game, setup)
    rep = job(db, setup, T + 20 * M)
    [d] = rows(db, "paper_strategy_decisions")
    assert (d["decision_status"], d["no_bet_reason"]) == ("no_bet", "decision_window_missed")
    assert rep.strategies[SID]["missed"] == 1 and rows(db, "paper_strategy_bets") == []
    job(db, setup, T + 25 * M)
    assert len(rows(db, "paper_strategy_decisions")) == 1


def test_dry_run_writes_nothing(db, game, setup):
    prepare_bet(db, game, setup)
    rep = job(db, setup, T + 2 * M, dry_run=True)
    assert rep.decisions and rep.decisions[0]["decision_status"] == "bet"
    for t in ("paper_strategy_decisions", "paper_strategy_bets", "paper_strategy_days", "bets"):
        assert rows(db, t) == []


# ======================= settlement / immutability ======================= #

def test_settlement_then_immutable(db, game, setup):
    _, oh = prepare_bet(db, game, setup)
    job(db, setup, T + 2 * M)
    ledger.paper_settlement_job(TIP + 30 * M, db=db)
    assert rows(db, "paper_strategy_bets")[0]["settlement_status"] == "pending"      # 尚未 final
    finalize(db, game)                                                               # 110–100 主勝
    rep = ledger.paper_settlement_job(TIP + 4 * timedelta(hours=1), db=db)
    [b] = rows(db, "paper_strategy_bets")
    assert rep.settled == 1 and b["settlement_status"] == "settled_win" and b["settled_at"] == TIP + timedelta(hours=4)
    assert b["profit_units"] == pytest.approx(b["stake_units"] * (oh - 1), rel=1e-12)
    assert (b["home_score"], b["away_score"], b["actual_value"], b["settlement_version"]) == (110, 100, 10.0, "settle-v1")
    finalize(db, game, home=(20, 20, 20, 20))                                         # 比分事後被改 → 已結算的不重算
    ledger.paper_settlement_job(TIP + timedelta(hours=5), db=db)
    assert rows(db, "paper_strategy_bets")[0]["settlement_status"] == "settled_win"
    for sql in ("UPDATE paper_strategy_bets SET settlement_status = 'settled_loss'",
                "UPDATE paper_strategy_bets SET profit_units = 0",
                "DELETE FROM paper_strategy_bets",
                "UPDATE paper_strategy_decisions SET decision_status = 'no_bet', no_bet_reason = 'no_odds'",
                "DELETE FROM paper_strategy_decisions",
                "UPDATE paper_strategy_days SET day_start_bankroll = 2"):
        with pytest.raises(psycopg.errors.RaiseException):
            with db.cursor() as cur:
                cur.execute(sql)


def test_pending_bet_execution_fields_immutable(db, game, setup):
    prepare_bet(db, game, setup)
    job(db, setup, T + 2 * M)
    for sql in ("UPDATE paper_strategy_bets SET stake_units = 1", "UPDATE paper_strategy_bets SET decimal_odds = 9"):
        with pytest.raises(psycopg.errors.RaiseException):
            with db.cursor() as cur:
                cur.execute(sql)


def test_day_start_frozen_and_next_day_uses_settled_bankroll(db, game, setup, seeded):
    _, oh = prepare_bet(db, game, setup)
    job(db, setup, T + 2 * M)
    finalize(db, game)
    ledger.paper_settlement_job(TIP + timedelta(hours=3), db=db)
    [b1] = rows(db, "paper_strategy_bets")
    tip2 = TIP + timedelta(days=1)
    g2 = second_game(db, seeded, tip2)
    t2 = tip2 - timedelta(hours=1)
    insert_pred(db, g2, setup, t2 - 30 * M, kind="final")
    write_obs(db, g2, S(MONEYLINE, prices={"home": oh, "away": round(0.97 / (1 - 1 / oh), 2)}), t2 - 20 * M)
    job(db, setup, t2 + 1 * M)
    days = rows(db, "paper_strategy_days")
    assert [d["betting_day"] for d in days] == [date(2026, 10, 22), date(2026, 10, 23)]
    assert days[0]["day_start_bankroll"] == 1.0
    assert days[1]["day_start_bankroll"] == pytest.approx(1.0 + b1["profit_units"], rel=1e-12)
    assert days[1]["prior_resolved_profit"] == pytest.approx(b1["profit_units"]) and days[1]["prior_unresolved_stake"] == 0
    d2 = next(d for d in rows(db, "paper_strategy_decisions") if d["game_id"] == g2)
    assert d2["day_start_bankroll"] == days[1]["day_start_bankroll"]
    perf = ledger.paper_performance(db=db)
    assert perf["evidence"]["evidence_class"] == "prospective_paper"
    assert perf["evidence"]["is_taiwan_sports_lottery_evidence"] is True
    assert perf["metrics"]["n_decisions"] == 2 and perf["metrics"]["wins"] == 1
    assert perf["bootstrap"]["status"] == "insufficient_sample"


def test_unsettled_prior_stake_excluded_from_next_day_start(db, game, setup, seeded):
    prepare_bet(db, game, setup)
    job(db, setup, T + 2 * M)                                                        # 第一天的注單仍 pending
    [b1] = rows(db, "paper_strategy_bets")
    tip2 = TIP + timedelta(days=1)
    g2 = second_game(db, seeded, tip2)
    job(db, setup, tip2 - timedelta(minutes=58))
    day2 = rows(db, "paper_strategy_days")[1]
    assert day2["day_start_bankroll"] == pytest.approx(1.0 - b1["stake_units"], rel=1e-12)
    assert day2["prior_unresolved_stake"] == pytest.approx(b1["stake_units"])


# ======================= prospective ↔ historical ======================= #

def test_prospective_decision_equals_historical_reconstruction(db, game, setup):
    """之後以歷史引擎重建同一 T：decision、定價 / sizing fingerprint、stake 完全一致——
    即使 T 之後同內容的輪詢把 last_seen_at 推到 T 之後（以 T 以前的輪詢紀錄判定新鮮度）。"""
    insert_pred(db, game, setup, T - 30 * M, kind="final")
    oh, oa = generous_ml(setup)
    snap = S(MONEYLINE, prices={"home": oh, "away": oa})
    write_obs(db, game, snap, T - 90 * M)                                              # 首次觀測 T−90（> 60 分上限）
    write_obs(db, game, snap, T - 5 * M)                                              # 同內容 → last_seen = T−5
    job(db, setup, T + 2 * M)
    write_obs(db, game, snap, T + 10 * M)                                             # T 之後再確認 → last_seen = T+10
    [orow] = rows(db, "odds_snapshots")
    assert orow["last_seen_at"] == T + 10 * M
    [p] = rows(db, "paper_strategy_decisions")
    [pb] = rows(db, "paper_strategy_bets")
    finalize(db, game)
    res = run_strategy_backtest(PRIMARY_SCOPE, date(2026, 10, 22), date(2026, 10, 22), root=setup["root"], db=db)
    assert res["evidence"]["evidence_class"] == "historical_observed"
    assert res["evidence"]["evidence_label"] == "taiwan_sports_lottery_strategy"
    [h] = res["decisions"]
    assert h["decision_status"] == p["decision_status"] == "bet"
    assert h["pricing_fingerprint"] == p["pricing_fingerprint"] and h["sizing_fingerprint"] == p["sizing_fingerprint"]
    assert h["wagers"][0]["stake_fraction"] == pytest.approx(pb["stake_fraction"], rel=1e-12)
    assert res["metrics"]["wins"] == 1 and res["bootstrap"]["status"] == "insufficient_sample"
    # 若 T 以前的輪詢沒有涵蓋這場（無法證明 T 時新鮮），歷史重建保守退回 fetched_at → stale（不是 hindsight 的新鮮）
    with db.cursor() as cur:
        cur.execute("UPDATE odds_fetch_runs SET diagnostics = '{}'::jsonb WHERE fetched_at = %s", (T - 5 * M,))
    res2 = run_strategy_backtest(PRIMARY_SCOPE, date(2026, 10, 22), date(2026, 10, 22), root=setup["root"], db=db)
    assert res2["decisions"][0]["no_bet_reason"] == "stale_odds"


# ======================= historical evidence ======================= #

def test_backtest_without_observed_odds_has_no_evidence(db, game, setup):
    insert_pred(db, game, setup, T - 30 * M, kind="final")
    with db.cursor() as cur:                                                         # seed 形狀（無 content_hash / run）
        cur.execute("""INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds)
                       VALUES (%s, %s, 'twsport', 'ml', NULL, 2.5, 1.5)""", (T - 20 * M, game))
    finalize(db, game)
    res = run_strategy_backtest(PRIMARY_SCOPE, date(2026, 10, 22), date(2026, 10, 22), root=setup["root"], db=db)
    assert res["evidence"]["evidence_class"] == "none" and res["evidence"]["historical_evidence_available"] is False
    assert res["metrics"] is None and "yield" not in str(res.get("metrics"))
    assert res["evidence"]["rejected_odds_rows"] == {"seed_fixture:legacy_or_seed_row": 1}
    empty = run_strategy_backtest(PRIMARY_SCOPE, date(2025, 1, 1), date(2025, 1, 2), root=setup["root"], db=db)
    assert empty["evidence"]["historical_evidence_available"] is False and empty["metrics"] is None


def test_oddsapi_backtest_is_international_diagnostic_only(db, game, setup):
    insert_pred(db, game, setup, T - 30 * M, kind="final")
    oh, oa = generous_ml(setup)
    write_obs(db, game, make_snapshot(source="oddsapi", bookmaker="pinnacle", source_event_id="X", market_type=MONEYLINE,
                                      period=FULL_GAME, status=OPEN, prices={"home": oh, "away": oa}), T - 20 * M)
    finalize(db, game)
    pin = run_strategy_backtest(parse_scope("oddsapi:pinnacle"), date(2026, 10, 22), date(2026, 10, 22),
                                root=setup["root"], db=db)
    e = pin["evidence"]
    assert e["evidence_class"] == "historical_observed" and e["evidence_label"] == "international_market_diagnostic"
    assert e["is_taiwan_sports_lottery_evidence"] is False and pin["metrics"]["n_bets"] == 1
    assert all(w["source"] == "oddsapi" and w["bookmaker"] == "pinnacle" for d in pin["decisions"] for w in d["wagers"])
    tw = run_strategy_backtest(PRIMARY_SCOPE, date(2026, 10, 22), date(2026, 10, 22), root=setup["root"], db=db)
    assert tw["evidence"]["historical_evidence_available"] is False and tw["metrics"] is None   # 不拿 Odds API 冒充台彩
    dk = run_strategy_backtest(parse_scope("oddsapi:draftkings"), date(2026, 10, 22), date(2026, 10, 22),
                               root=setup["root"], db=db)
    assert dk["metrics"] is None                                                      # 不跨 bookmaker 借價格


# ======================= artifact immutability（真實 artifact） ======================= #

def test_tampered_artifact_is_unavailable(setup, tmp_path):
    root = tmp_path / "arts"
    shutil.copytree(setup["root"], root)
    v = setup["art_a"].artifact_version
    bundle = next((root / "versions" / v).glob("*.joblib"))
    bundle.write_bytes(bundle.read_bytes() + b"tamper")
    from test_pricing_artifact import pred_row
    snap = S(MONEYLINE, prices={"home": 1.9, "away": 1.9})
    from core.execution.fixture import odds_row
    g = {"id": 9001, "date_utc": TIP}
    rec = asof.reconstruct_game(
        g, T, scope=PRIMARY_SCOPE, odds_rows=[odds_row(1, 9001, snap, T - 20 * M, T - 5 * M)],
        pred_rows=[pred_row(setup["gp"], created=T - 30 * M)], runs=[], risk=RISK_V1, policy=EXECUTION_V1,
        model_for=pricing_engine.ArtifactCache(root).provider, artifacts=asof.LocalArtifactRegistry(root))
    assert rec.pre_reason == "artifact_unavailable" and rec.artifact_error.startswith("artifact_load_failed")
    ok = asof.reconstruct_game(
        g, T, scope=PRIMARY_SCOPE, odds_rows=[odds_row(1, 9001, snap, T - 20 * M, T - 5 * M)],
        pred_rows=[pred_row(setup["gp"], created=T - 30 * M)], runs=[], risk=RISK_V1, policy=EXECUTION_V1,
        model_for=pricing_engine.ArtifactCache(setup["root"]).provider,
        artifacts=asof.LocalArtifactRegistry(setup["root"]))
    assert ok.pre_reason is None and ok.pricings[0].status == "priced"
