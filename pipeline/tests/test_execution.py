"""Phase D.4：execution-v1（as-of 重建 / 決策 / sequential exposure / 結算 / 帳務 / metrics / bootstrap / evidence）——純邏輯，不連 DB

合成 validation slate（core.execution.fixture）的 bankroll 路徑以 Fraction 手算（fixture.EXPECTED），逐項核對。
"""
from __future__ import annotations

import ast
import copy
import math
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from core.execution import asof, evidence, metrics, settlement
from core.execution.engine import (BET, NO_BET, DayLedger, DayState, Wager, available_bankroll, day_ledgers,
                                   execute_batch, simulate)
from core.execution.fixture import (D1_T1, EXPECTED, FIXTURE_ARTIFACT, FixtureModel, build_fixture, odds_row,
                                    pred_row, run_fixture_validation, tw)
from core.execution.ledger import display_amounts
from core.execution.policy import (EXECUTION_V1, GAME_T_MINUS_MINUTES, PRIMARY_SCOPE, ExecutionPolicy,
                                   StrategyScope, assert_registered, parse_scope, strategy_id)
from core.odds.canonical import MONEYLINE
from core.pricing import engine as pricing_engine
from core.pricing.alignment import latest_snapshots_as_of, price_game_as_of
from core.sizing.job import size_pricings
from core.sizing.policy import RISK_V1

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[2]
F = Fraction


def run(fx=None, scope=PRIMARY_SCOPE, **kw):
    fx = fx or build_fixture()
    return simulate(fx.games, fx.odds_rows, fx.pred_rows, fx.runs, scope=scope, model_for=fx.model_for,
                    artifacts=fx.artifacts, **kw)


def dec(sim, gid):
    return next(d for d in sim.decisions if d.game_id == gid)


def wager(sim, gid, market, side):
    return next(w for w in sim.wagers if (w.game_id, w.market, w.side) == (gid, market, side))


def approx(x):
    return pytest.approx(float(x), rel=1e-12, abs=1e-15)


def reconstruct(fx, gid, t=None, **kw):
    g = next(g for g in fx.games if g["id"] == gid)
    t = t or asof.decision_time(g["date_utc"], EXECUTION_V1)
    args = dict(scope=PRIMARY_SCOPE, odds_rows=fx.odds_rows, pred_rows=fx.pred_rows, runs=fx.runs, risk=RISK_V1,
                policy=EXECUTION_V1, model_for=fx.model_for, artifacts=fx.artifacts)
    args.update(kw)
    return asof.reconstruct_game(g, t, **args)


@pytest.fixture(scope="module")
def sim():
    return run()


# ======================= policy（凍結） ======================= #

def test_execution_v1_is_frozen():
    p = EXECUTION_V1
    assert p.version == "execution-v1" and GAME_T_MINUS_MINUTES == 60
    assert p.decision_offset == timedelta(minutes=60) and p.starting_bankroll_units == 1.0
    assert p.betting_day_timezone == "Asia/Taipei" and p.risk_policy_version == "risk-v1"
    with pytest.raises(Exception):
        p.decision_offset = timedelta(minutes=15)                                   # frozen dataclass
    assert_registered(EXECUTION_V1, RISK_V1)
    for alt in (replace(p, decision_offset=timedelta(minutes=15)), replace(p, max_evaluation_lag=timedelta(hours=2))):
        with pytest.raises(ValueError):
            assert_registered(alt, RISK_V1)                                          # 冒充 execution-v1 → 拒絕
    with pytest.raises(ValueError):
        assert_registered(EXECUTION_V1, replace(RISK_V1, max_bet_fraction=0.05))


def test_no_timing_or_ev_threshold_knobs_in_cli_or_env():
    """execution timing / 最低 EV / bookmaker 混用都不能從 CLI、環境變數調整。"""
    for f in ("run_strategy_backtest.py", "run_paper.py"):
        src = (REPO / "pipeline" / f).read_text()
        flags = set(re.findall(r'add_argument\("(--[a-z-]+)"', src))
        assert not flags & {"--minutes", "--offset", "--decision-offset", "--t-minus", "--min-ev", "--ev-threshold",
                            "--best-book", "--consensus", "--kelly", "--max-bet"}
    for f in (REPO / "pipeline" / "core" / "execution").glob("*.py"):
        assert "os.environ" not in f.read_text() and "settings." not in f.read_text()


def test_strategy_scope_single_bookmaker_and_labels():
    assert PRIMARY_SCOPE.kind == "primary" and PRIMARY_SCOPE.evidence_label == "taiwan_sports_lottery_strategy"
    pin = parse_scope("oddsapi:pinnacle")
    assert pin.kind == "diagnostic" and pin.evidence_label == "international_market_diagnostic"
    for bad in (("oddsapi", "best"), ("oddsapi", "*"), ("oddsapi", "consensus"), ("oddsapi", "twsport"),
                ("twsport", "pinnacle"), ("pinnacle", "pinnacle")):
        with pytest.raises(ValueError):
            StrategyScope(*bad)
    with pytest.raises(ValueError):
        parse_scope("oddsapi")                                                       # 必須指定單一 bookmaker
    assert strategy_id(EXECUTION_V1, RISK_V1, pin) != strategy_id(EXECUTION_V1, RISK_V1, PRIMARY_SCOPE)
    assert "execution-v1" in strategy_id(EXECUTION_V1, RISK_V1, PRIMARY_SCOPE)


# ======================= as-of reconstruction ======================= #

def test_decision_time_is_exact_t_minus_60(sim):
    for d in sim.decisions:
        assert d.decision_time == d.scheduled_tipoff - timedelta(minutes=60)
    assert dec(sim, 101).decision_time == D1_T1


def test_exact_t60_reconstruction_equals_direct_d2_d3_pure_functions():
    """as-of 重建 = 直接以 T 呼叫 D.2 price_game_as_of + D.3 size_pricings（不讀任何 cache 表）。"""
    fx = build_fixture()
    rec = reconstruct(fx, 101)
    rows = [r for r in fx.odds_rows if r["game_id"] == 101]
    direct = price_game_as_of(rows, fx.pred_rows, 101, D1_T1, fx.model_for)
    assert [mp.to_dict() for mp in rec.pricings] == [mp.to_dict() for mp in direct]
    s1 = size_pricings(rec.pricings, {101: rec.scheduled_tipoff}, D1_T1)
    s2 = size_pricings(direct, {101: rec.scheduled_tipoff}, D1_T1)
    assert [x.to_dict() for x in s1] == [x.to_dict() for x in s2]
    assert 10199 not in rec.odds_snapshot_ids                                        # T 之後的較佳賠率不在其中


def test_data_after_t_never_used():
    """把所有 T 之後才出現的盤口 / 預測刪掉、或加入荒謬的未來資料 → 全部 decision 完全相同。"""
    base = run()
    fx = build_fixture()
    t_of = {g["id"]: asof.decision_time(g["date_utc"], EXECUTION_V1) for g in fx.games}
    fx.odds_rows = [r for r in fx.odds_rows if r["fetched_at"] <= t_of[r["game_id"]]]
    fx.pred_rows = [p for p in fx.pred_rows if p["created_at"] <= t_of[p["game_id"]]]
    stripped = run(fx)
    fx2 = build_fixture()
    for g in fx2.games:                                                              # 未來資料：極端賠率 + 新預測
        t = t_of[g["id"]]
        fx2.odds_rows.append(odds_row(900000 + g["id"], g["id"], tw(MONEYLINE, prices={"home": 50.0, "away": 1.01}),
                                      t + timedelta(seconds=1), t + timedelta(minutes=30)))
        fx2.pred_rows.append(pred_row(900000 + g["id"], g["id"], t + timedelta(seconds=1)))
    future = run(fx2)
    for a, b, c in zip(base.decisions, stripped.decisions, future.decisions):
        assert a.to_dict() == b.to_dict() == c.to_dict()


def test_job_running_after_t_still_reconstructs_exact_t():
    """job 晚 3 分鐘執行：analysis_as_of 仍是 T；evaluated_at 只是稽核欄位。"""
    fx = build_fixture()
    t = D1_T1
    late_row = odds_row(777, 101, tw(MONEYLINE, prices={"home": 3.0, "away": 1.4}), t + timedelta(minutes=1))
    fx.odds_rows.append(late_row)
    recs = [reconstruct(fx, gid, t) for gid in (101, 102)]
    a = execute_batch(recs, t, DayState(date(2026, 10, 22), 1.0), sid="s", evaluated_at=t + timedelta(minutes=3))
    b = execute_batch([reconstruct(build_fixture(), gid, t) for gid in (101, 102)], t, DayState(date(2026, 10, 22), 1.0),
                      sid="s", evaluated_at=t)
    for x, y in zip(a, b):
        assert x.decision_time == y.decision_time == t and x.evaluated_at == t + timedelta(minutes=3)
        dx, dy = x.to_dict(), y.to_dict()
        dx.pop("evaluated_at"), dy.pop("evaluated_at")
        assert dx == dy and 777 not in x.odds_snapshot_ids


def test_no_later_quote_backfill(sim):
    d = dec(sim, 109)                                                               # 盤口 T+10 分才出現
    assert (d.decision_status, d.no_bet_reason) == (NO_BET, "no_odds") and d.odds_snapshot_ids == []


def test_no_later_prediction_backfill(sim):
    d = dec(sim, 105)                                                               # 預測 T+5 分才寫入
    assert (d.decision_status, d.no_bet_reason) == (NO_BET, "no_prediction") and d.prediction_id is None
    assert d.odds_snapshot_ids                                                       # 盤口是有的


def test_last_seen_after_t_is_not_hindsight_confirmation():
    """odds_snapshots.last_seen_at 只存「最後一次」確認。若它 > T，不能拿 T 之後的確認證明 T 時報價新鮮。"""
    fx = build_fixture()
    t = D1_T1
    for r in fx.odds_rows:
        if r["game_id"] == 101 and r["id"] != 10199:
            r["fetched_at"], r["last_seen_at"] = t - timedelta(minutes=100), t + timedelta(minutes=5)
    rec = reconstruct(fx, 101, t)
    assert rec.pre_reason == asof.STALE_ODDS                                         # 只知道 T−100 分的觀測 → stale
    # T 以前有一次成功輪詢涵蓋這場比賽 → 該次輪詢才是 T 時點的最後確認 → 新鮮
    run_ok = {"id": 1, "source": "twsport", "fetched_at": t - timedelta(minutes=10), "outcome": "success",
              "diagnostics": {"events": [{"game_id": 101, "status": "matched", "n_snapshots": 2}]}}
    assert reconstruct(fx, 101, t, runs=[run_ok]).pre_reason is None
    # 輪詢沒涵蓋這場 / 失敗 / 別的來源 / 在 T 之後 → 不算
    for bad in ({**run_ok, "diagnostics": {"events": [{"game_id": 999, "status": "matched", "n_snapshots": 2}]}},
                {**run_ok, "outcome": "blocked"}, {**run_ok, "source": "oddsapi"},
                {**run_ok, "fetched_at": t + timedelta(minutes=1)}):
        assert reconstruct(fx, 101, t, runs=[bad]).pre_reason == asof.STALE_ODDS
    row = next(r for r in fx.odds_rows if r["game_id"] == 101 and r["id"] != 10199)
    assert asof.confirmed_last_seen_as_of(row, t, [run_ok]) == t - timedelta(minutes=10)
    assert asof.confirmed_last_seen_as_of(row, t + timedelta(minutes=10), []) == t + timedelta(minutes=5)


def test_stale_rule_matches_d3_freshness():
    """asof.quote_is_stale 與 D.3 sizing 的 stale 判定一致（同一 risk-v1 規則，不另訂門檻）。"""
    fx = build_fixture()
    rec = reconstruct(fx, 106)
    res = size_pricings(rec.pricings, {106: rec.scheduled_tipoff}, rec.decision_time)
    assert rec.pre_reason == asof.STALE_ODDS
    assert {r.qualification_status for r in res if r.side == "home"} == {"stale_quote"}
    t = rec.decision_time
    for age_min, stale in ((59, False), (60, False), (61, True)):
        assert asof.quote_is_stale("twsport", t - timedelta(hours=3), t - timedelta(minutes=age_min), t, RISK_V1) is stale
    assert asof.quote_is_stale("unknown_source", t, t, t, RISK_V1) is True


def test_artifact_must_exist_before_t_and_load():
    fx = build_fixture()
    late = asof.StaticArtifactRegistry({FIXTURE_ARTIFACT: D1_T1 + timedelta(minutes=1)})
    rec = reconstruct(fx, 101, artifacts=late)
    assert rec.pre_reason == asof.ARTIFACT_UNAVAILABLE and rec.artifact_error == "artifact_created_after_decision_time"
    assert all(mp.status != pricing_engine.PRICED for mp in rec.pricings)              # 不用模型
    rec = reconstruct(fx, 101, artifacts=asof.StaticArtifactRegistry({}))
    assert rec.pre_reason == asof.ARTIFACT_UNAVAILABLE and rec.artifact_error == "artifact_missing"

    def broken(pred):
        raise pricing_engine.ModelProbabilityError("bundle 雜湊與 manifest 不符")
    rec = reconstruct(fx, 101, model_for=broken)
    assert rec.pre_reason == asof.ARTIFACT_UNAVAILABLE and rec.artifact_error.startswith("artifact_load_failed")


def test_scope_filters_other_bookmakers_before_pricing():
    """台彩策略看不到 Odds API 的報價（不做 best-book）；Odds API 診斷看不到台彩報價。"""
    fx = build_fixture()
    from core.odds.canonical import make_snapshot, OPEN, FULL_GAME
    pin = make_snapshot(source="oddsapi", bookmaker="pinnacle", source_event_id="P", market_type=MONEYLINE,
                        period=FULL_GAME, status=OPEN, prices={"home": 3.0, "away": 1.45})
    fx.odds_rows.append(odds_row(5555, 108, pin, D1_T1, D1_T1))
    tw_sim, pin_sim = run(fx), run(fx, scope=parse_scope("oddsapi:pinnacle"))
    assert 5555 not in dec(tw_sim, 108).odds_snapshot_ids and dec(tw_sim, 108).no_bet_reason == "no_positive_ev"
    assert all(w.source == "twsport" for w in tw_sim.wagers)
    assert all(w.source == "oddsapi" and w.bookmaker == "pinnacle" for w in pin_sim.wagers)
    assert dec(pin_sim, 101).no_bet_reason == "no_odds"                              # 台彩列不會被當成 pinnacle


# ======================= decisions ======================= #

def test_every_game_has_exactly_one_decision(sim):
    fx = build_fixture()
    assert sorted(d.game_id for d in sim.decisions) == sorted(g["id"] for g in fx.games)
    for gid, (status, reason) in EXPECTED["decisions"].items():
        d = dec(sim, gid)
        assert (d.decision_status, d.no_bet_reason) == (status, reason), gid
        assert (d.decision_status == BET) == bool(d.wagers)
        assert d.pricing_fingerprint and d.sizing_fingerprint


def test_no_positive_ev_and_stale_and_positive_sizing(sim):
    assert dec(sim, 108).no_bet_reason == "no_positive_ev"
    d106 = dec(sim, 106)
    assert d106.no_bet_reason == "stale_odds" and any(s["qualification_status"] == "stale_quote" for s in d106.sizing)
    d107 = dec(sim, 107)
    assert d107.decision_status == BET and [w.market for w in d107.wagers] == ["ml"]   # 讓分兩邊 EV < 0 → 不下
    w = d107.wagers[0]
    assert w.ev_per_unit == pytest.approx(0.04) and w.stake_fraction == approx(F(1, 100))


def test_only_actionable_positive_stakes_are_executed(sim):
    for d in sim.decisions:
        exec_keys = {(w.odds_snapshot_id, w.side) for w in d.wagers}
        for s in d.sizing:
            if (s["odds_snapshot_id"], s["side"]) in exec_keys:
                assert s["actionable"] and s["final_stake_fraction"] > 0 and s["ev_per_unit"] > 0
    for w in sim.wagers:
        assert 0 < w.stake_fraction <= w.sizing_final_stake_fraction <= RISK_V1.max_bet_fraction + 1e-15


def test_no_minimum_ev_threshold():
    """EV > 0 + actionable 即執行（極小 EV 得到極小 stake）；不因 EV 小而被濾掉。"""
    fx = build_fixture()
    fx.tables[107][("margin", 0.0)] = (0.5005, 0.0, 0.4995)                          # EV = +0.001
    d = dec(run(fx), 107)
    assert d.decision_status == BET and d.wagers[0].stake_fraction == pytest.approx(0.25 * 0.001, rel=1e-9)


# ======================= sequential day exposure ======================= #

def test_day_start_fixed_during_day_and_committed_stakes_consume_budget(sim):
    d1 = [d for d in sim.decisions if d.betting_day == date(2026, 10, 22)]
    assert {d.day_start_bankroll for d in d1} == {1.0}
    d103 = dec(sim, 103)
    committed = F(3, 100) + F(2, 100) + F(1, 220)
    assert d103.committed_fraction_before == approx(committed)
    assert d103.remaining_day_fraction_before == approx(F(8, 100) - committed) == approx(F(7, 275))
    assert d103.execution_scale_factor == approx(EXPECTED["execution_scale_103"])
    for w in d103.wagers:
        assert w.sizing_final_stake_fraction == approx(F(15, 1000))                   # D.3 static：同場 3% → 各 1.5%
        assert w.stake_fraction == approx(F(7, 550))
    assert math.fsum(w.stake_fraction for d in d1 for w in d.wagers) == pytest.approx(0.08, abs=1e-15)
    d104 = dec(sim, 104)
    assert d104.no_bet_reason == "portfolio_cap" and d104.remaining_day_fraction_before == pytest.approx(0, abs=1e-12)
    assert any(s["actionable"] for s in d104.sizing)                                 # D.3 單獨看是 actionable


def test_same_decision_time_is_one_batch_independent_of_game_id(sim):
    d101, d102 = dec(sim, 101), dec(sim, 102)
    assert d101.committed_fraction_before == d102.committed_fraction_before == 0.0
    assert d101.execution_scale_factor == d102.execution_scale_factor == 1.0


def test_same_day_settlement_does_not_increase_later_stakes():
    """G101 / G102 的結果（全贏 or 全輸）不影響同日稍後 G103 的 stake；只影響下一日 day_start。"""
    def with_results(home_wins: bool):
        fx = build_fixture()
        for g in fx.games:
            if g["id"] in (101, 102):
                hi, lo = (30, 20) if home_wins else (20, 30)
                g.update(home_pts=4 * hi, away_pts=4 * lo, home_h1=2 * hi, away_h1=2 * lo,
                         **{f"home_q{i}": hi for i in range(1, 5)}, **{f"away_q{i}": lo for i in range(1, 5)})
        return run(fx)
    win, lose = with_results(True), with_results(False)
    for gid in (103,):
        assert [w.stake_units for w in dec(win, gid).wagers] == [w.stake_units for w in dec(lose, gid).wagers]
    assert dec(win, 107).day_start_bankroll > dec(lose, 107).day_start_bankroll       # 次日才反映


def test_next_day_bankroll_includes_prior_day_settlement(sim):
    assert dec(sim, 107).day_start_bankroll == approx(EXPECTED["day_end"]["2026-10-22"])
    w = wager(sim, 107, "ml", "home")
    assert w.stake_units == approx(EXPECTED["day_end"]["2026-10-22"] * F(1, 100))


def test_unresolved_prior_stake_not_added_back():
    """前一日 ungradable（上半場比分缺）→ 該 stake 不回到下一日 day_start（不猜結果）。"""
    fx = build_fixture()
    for g in fx.games:
        if g["id"] == 102:
            g["home_h1"] = g["away_h1"] = None
            g["home_q1"] = g["home_q2"] = g["away_q1"] = g["away_q2"] = None
            g["home_q3"] = g["home_q4"] = g["away_q3"] = g["away_q4"] = None
    s = run(fx)
    h1 = wager(s, 102, "h1_ml", "draw")
    assert h1.settlement_status == "ungradable" and h1.settlement_reason == "missing_h1_score"
    day1 = s.days[0]
    assert not day1.closed and day1.day_end_bankroll is None and day1.unresolved_stake == approx(F(1, 220))
    expected = 1 + F(15, 1000) - F(2, 100) - F(14, 550) - F(1, 220)                 # 不含和局獎金、也不含其本金
    assert dec(s, 107).day_start_bankroll == approx(expected) == approx(day1.day_close_bankroll)
    m = metrics.strategy_metrics(s.decisions, s.days, 1.0)
    assert m["ungradable"] == 1 and m["bankroll_status"] == "provisional_unresolved"


def test_available_bankroll_formula():
    t = datetime(2026, 11, 1, tzinfo=UTC)
    mk = lambda st, stake, prof, at: Wager(  # noqa: E731
        "s", 1, date(2026, 10, 31), t, t, 1, None, "twsport", "twsport", "ml", "moneyline", "full_game", "two_way",
        "home", None, None, "margin", 0.0, "gt", "moneyline_ot_included", 2.0, 0.6, 0.0, 0.4, 0.2, stake, 1.0, stake,
        stake, stake * 0.2, settlement_status=st, profit_units=prof, resolved_at=at)
    ws = [mk("settled_win", 0.02, 0.02, t - timedelta(hours=1)), mk("settled_loss", 0.01, -0.01, t - timedelta(hours=2)),
          mk("settled_win", 0.03, 0.03, t + timedelta(hours=1)), mk("pending", 0.015, None, None)]
    assert available_bankroll(1.0, ws, t) == pytest.approx(1.0 + 0.02 - 0.01 - 0.03 - 0.015)


# ======================= settlement ======================= #

GAME = {"status": "final", "home_pts": 110, "away_pts": 104, "home_h1": 55, "away_h1": 55,
        "home_q1": 30, "home_q2": 25, "home_q3": 25, "home_q4": 30, "away_q1": 28, "away_q2": 27, "away_q3": 24,
        "away_q4": 25}


def bet(target="margin", thr=0.0, cmp="gt", rule="moneyline_ot_included", odds=2.0, stake=0.01, **kw):
    b = {"model_target": target, "model_threshold": thr, "comparator": cmp, "settlement_rule": rule,
         "decimal_odds": odds, "stake_units": stake, "market_type": "moneyline", "period": "full_game",
         "outcome_set": "two_way"}
    b.update(kw)
    return b


@pytest.mark.parametrize("b,status", [
    (bet(), "settled_win"), (bet(cmp="lt"), "settled_loss"),                                           # ML 110–104
    (bet(thr=5.5, rule="half_line_no_push", market_type="spread"), "settled_win"),                     # 主 −5.5
    (bet(thr=6.5, rule="half_line_no_push", market_type="spread"), "settled_loss"),
    (bet(thr=6.0, rule="integer_line_push_refund", market_type="spread"), "settled_push"),             # 主 −6 push
    (bet(thr=6.0, cmp="lt", rule="integer_line_push_refund", market_type="spread"), "settled_push"),
    (bet(thr=7.0, cmp="lt", rule="integer_line_push_refund", market_type="spread"), "settled_win"),    # 客 +7
    (bet("total", 213.5, "gt", "half_line_no_push", market_type="total"), "settled_win"),              # 214 大
    (bet("total", 214.0, "gt", "integer_line_push_refund", market_type="total"), "settled_push"),
    (bet("total", 214.0, "lt", "integer_line_push_refund", market_type="total"), "settled_push"),
    (bet("total", 215.5, "lt", "half_line_no_push", market_type="total"), "settled_win"),              # 小
    (bet("h1_margin", 0.5, "gt", "half_line_no_push", market_type="spread", period="h1"), "settled_loss"),
    (bet("h1_margin", -1.0, "gt", "integer_line_push_refund", market_type="spread", period="h1"), "settled_win"),
    (bet("h1_total", 110.0, "gt", "integer_line_push_refund", market_type="total", period="h1"), "settled_push"),
    (bet("h1_total", 109.5, "lt", "half_line_no_push", market_type="total", period="h1"), "settled_loss"),
    (bet("h1_margin", 0.0, "eq", "three_way_draw_outcome", period="h1", outcome_set="three_way"), "settled_draw_win"),
    (bet("h1_margin", 0.0, "gt", "three_way_draw_outcome", period="h1", outcome_set="three_way"), "settled_loss"),
    (bet("h1_margin", 0.0, "lt", "three_way_draw_outcome", period="h1", outcome_set="three_way"), "settled_loss"),
])
def test_settlement_outcomes(b, status):
    s = settlement.settle(b, GAME)
    assert s.status == status, s
    assert s.profit_units == approx({"settled_win": 0.01, "settled_draw_win": 0.01, "settled_loss": -0.01,
                                     "settled_push": 0.0}[status])


def test_h1_three_way_draw_is_outcome_not_push():
    s = settlement.settle(bet("h1_margin", 0.0, "eq", "three_way_draw_outcome", odds=12.0, period="h1",
                              outcome_set="three_way"), GAME)
    assert s.status == "settled_draw_win" and s.profit_units == approx(0.11) and s.actual_value == 0.0
    home = settlement.settle(bet("h1_margin", 0.0, "gt", "three_way_draw_outcome", period="h1",
                                 outcome_set="three_way"), GAME)
    assert home.status == "settled_loss" and home.reason == "h1_draw_loses_home_away"


@pytest.mark.parametrize("game_patch,reason", [
    ({"home_h1": None, "away_h1": None}, "missing_h1_score"),
    ({"home_h1": 50}, "score_inconsistent:home_h1_vs_quarters"),
    ({"home_pts": 111}, "score_inconsistent:home_pts_vs_quarters"),
])
def test_missing_or_inconsistent_h1_is_ungradable(game_patch, reason):
    s = settlement.settle(bet("h1_total", 109.5, "gt", "half_line_no_push", market_type="total", period="h1"),
                          {**GAME, **game_patch})
    assert (s.status, s.reason, s.profit_units) == ("ungradable", reason, None)


def test_non_final_pending_and_cancel_postpone_ungradable_no_void_guess():
    assert settlement.settle(bet(), {**GAME, "status": "scheduled"}).status == "pending"
    assert settlement.settle(bet(), {**GAME, "status": "live"}).status == "pending"
    for st in ("cancelled", "postponed"):
        s = settlement.settle(bet(), {**GAME, "status": st})
        assert s.status == "ungradable" and "void_rule_unverified" in s.reason and s.profit_units is None
    assert settlement.VOID_RULES == {}                                               # 沒有未驗證的 void 假設
    tip = datetime(2026, 10, 21, 23, 0, tzinfo=UTC)
    s = settlement.settle(bet(), {**GAME, "date_utc": tip + timedelta(days=1)}, decision_scheduled_tipoff=tip)
    assert s.status == "ungradable" and s.reason.startswith("rescheduled_after_decision")
    s = settlement.settle(bet(), {**GAME, "date_utc": tip + timedelta(minutes=20)}, decision_scheduled_tipoff=tip)
    assert s.status == "settled_win"                                                 # 小幅延後不算改期


def test_unsupported_markets_never_settle_as_valid_bets():
    cases = [bet(rule="h1_two_way_tie_settlement_unknown", period="h1"),
             bet(target="h1_margin", period="h1", outcome_set="two_way", rule="moneyline_ot_included"),
             bet(thr=5.25, rule="quarter_line_split_settlement", market_type="spread"),
             bet(thr=5.25, rule="half_line_no_push", market_type="spread"),
             bet(rule="full_game_three_way_regulation_not_modelled", outcome_set="three_way"),
             bet(rule=None)]
    for b in cases:
        s = settlement.settle(b, GAME)
        assert s.status == "ungradable" and s.profit_units is None, b


def test_impossible_results_are_ungradable():
    tie = {**GAME, "home_pts": 104, "home_q4": 24}
    assert settlement.settle(bet(), tie).reason == "score_inconsistent:full_game_tie"
    s = settlement.settle(bet(thr=6.0, rule="half_line_no_push", market_type="spread"), GAME)
    assert s.status == "ungradable" and s.reason.startswith("score_on_no_push_line")


# ======================= accounting ======================= #

def test_payouts():
    assert settlement.profit("settled_win", 0.02, 1.91) == pytest.approx(0.02 * 0.91)
    assert settlement.profit("settled_draw_win", 0.01, 12.0) == pytest.approx(0.11)
    assert settlement.profit("settled_loss", 0.02, 1.91) == -0.02
    assert settlement.profit("settled_push", 0.02, 1.91) == 0.0 and settlement.profit("void", 0.02, 1.91) == 0.0
    assert settlement.profit("pending", 0.02, 1.91) is None and settlement.profit("ungradable", 0.02, 1.91) is None


def test_fixture_wagers_settlement_and_hand_computed_bankroll_path(sim):
    assert len(sim.wagers) == len(EXPECTED["wagers"])
    for (gid, market, side), (frac, status, prof) in EXPECTED["wagers"].items():
        w = wager(sim, gid, market, side)
        assert w.stake_fraction == approx(frac) and w.settlement_status == status
        assert w.profit_units == approx(prof)
    for d in sim.days:
        k = d.betting_day.isoformat()
        assert d.closed and d.day_start_bankroll == approx(EXPECTED["day_start"][k])
        assert d.day_profit == approx(EXPECTED["day_profit"][k]) and d.day_end_bankroll == approx(EXPECTED["day_end"][k])


def test_bankroll_conservation(sim):
    days = sim.days
    for a, b in zip(days, days[1:]):
        assert b.day_start_bankroll == pytest.approx(a.day_end_bankroll, abs=1e-15)
    for d in days:
        assert d.day_end_bankroll == pytest.approx(d.day_start_bankroll + d.day_profit, abs=1e-15)
        assert d.day_profit == pytest.approx(math.fsum(w.profit_units for w in sim.wagers
                                                       if w.betting_day == d.betting_day), abs=1e-15)
    m = metrics.strategy_metrics(sim.decisions, days, 1.0)
    assert m["ending_bankroll_units"] == pytest.approx(1.0 + math.fsum(d.day_profit for d in days), abs=1e-15)
    assert m["ending_bankroll_units"] == approx(EXPECTED["day_end"]["2026-10-23"])


def test_multi_bet_daily_pnl(sim):
    d1 = sim.days[0]
    assert d1.n_bets == 6 and d1.day_profit == approx(F(43, 2200))
    assert d1.stake_fraction == pytest.approx(0.08, abs=1e-15)


def test_normalized_vs_display_bankroll_proportionality(sim):
    """starting bankroll 只換算顯示金額；選擇與比例完全相同、金額等比例。"""
    big = run(starting_bankroll=10000.0)
    assert [d.decision_status for d in big.decisions] == [d.decision_status for d in sim.decisions]
    for a, b in zip(sim.wagers, big.wagers):
        assert a.stake_fraction == b.stake_fraction and b.stake_units == pytest.approx(10000 * a.stake_units, rel=1e-12)
        assert b.profit_units == pytest.approx(10000 * a.profit_units, rel=1e-12, abs=1e-12)
    m = metrics.strategy_metrics(sim.decisions, sim.days, 1.0)
    disp = display_amounts(m, 10000)
    mb = metrics.strategy_metrics(big.decisions, big.days, 10000.0)
    assert disp["ending_bankroll"] == pytest.approx(mb["ending_bankroll_units"], rel=1e-12)
    assert mb["yield"] == pytest.approx(m["yield"], rel=1e-12) and mb["bankroll_return"] == pytest.approx(
        m["bankroll_return"], rel=1e-12)


# ======================= metrics ======================= #

def test_metrics_exact(sim):
    m = metrics.strategy_metrics(sim.decisions, sim.days, 1.0)
    staked = F(15, 1000) * 2 + F(2, 100) + F(1, 220) + F(7, 550) * 2 + EXPECTED["day_end"]["2026-10-22"] / 100
    net = EXPECTED["day_profit"]["2026-10-22"] + EXPECTED["day_profit"]["2026-10-23"]
    assert m["total_staked_units"] == approx(staked) and m["net_profit_units"] == approx(net)
    assert m["yield"] == approx(net / staked)
    assert m["bankroll_return"] == approx(EXPECTED["day_end"]["2026-10-23"] - 1)
    assert m["log_bankroll_growth"] == pytest.approx(math.log(float(EXPECTED["day_end"]["2026-10-23"])), rel=1e-12)
    assert (m["n_decisions"], m["n_bet_decisions"], m["n_no_bet"], m["n_bets"]) == (9, 4, 5, 7)
    assert m["action_rate"] == pytest.approx(4 / 9)
    assert (m["wins"], m["draw_wins"], m["losses"], m["pushes"], m["void"], m["ungradable"]) == (3, 1, 3, 1, 0, 0)
    assert m["no_bet_reasons"] == {"no_odds": 1, "no_positive_ev": 1, "no_prediction": 1, "portfolio_cap": 1,
                                   "stale_odds": 1}
    exp = math.fsum(w.stake_units * w.ev_per_unit for w in sim.wagers)
    assert m["expected_profit_units"] == pytest.approx(exp, rel=1e-12)
    assert m["realized_minus_expected_units"] == pytest.approx(float(net) - exp, rel=1e-9)
    assert m["best_betting_day"]["betting_day"] == "2026-10-22" and m["worst_betting_day"]["betting_day"] == "2026-10-23"


def test_yield_and_bankroll_return_are_distinct():
    sim = run()
    m = metrics.strategy_metrics(sim.decisions, sim.days, 1.0)
    assert "roi" not in {k.lower() for k in m}
    assert m["yield"] != pytest.approx(m["bankroll_return"])


def test_max_drawdown_exact():
    dd = metrics.max_drawdown([1.0, 1.1, 0.99, 1.05, 0.88, 1.2, 1.0])
    assert dd["max_drawdown_units"] == pytest.approx(0.22) and dd["max_drawdown_pct"] == pytest.approx(0.2)
    assert metrics.max_drawdown([1.0, 1.1, 1.2])["max_drawdown_units"] == 0.0


def test_drawdown_uses_day_close_path():
    days = [DayLedger(date(2026, 11, i), s, 1, 1, 0.02, 0.02, p, 0.0, s + p, s + p, True)
            for i, (s, p) in enumerate([(1.0, 0.05), (1.05, -0.08), (0.97, -0.02), (0.95, 0.1)], start=1)]
    dd = metrics.max_drawdown(metrics.bankroll_path(1.0, days))
    assert dd["max_drawdown_units"] == pytest.approx(0.10) and dd["max_drawdown_pct"] == pytest.approx(0.10 / 1.05)


# ======================= bootstrap ======================= #

def _synthetic_days(n_days: int, *, hedge: bool):
    """每日兩筆 bet。hedge=True：同日一贏一輸（日損益 0）——逐 bet 重抽會有變異，以日為單位重抽則恆為 0。"""
    decisions = []
    sim = run()
    proto = sim.wagers[0]
    for i in range(n_days):
        d = date(2026, 11, 1) + timedelta(days=i)
        ws = []
        for j, st in enumerate(("settled_win", "settled_loss")):
            win = st == "settled_win" if hedge else (i + j) % 3 != 0
            odds = 2.0 if hedge else 1.5 + 0.037 * i + 0.011 * j                     # 非對沖：連續值，避免分位數落在同一格
            ws.append(replace(proto, game_id=1000 + 2 * i + j, betting_day=d, decimal_odds=odds, stake_units=0.01,
                              settlement_status="settled_win" if win else "settled_loss",
                              profit_units=0.01 * (odds - 1) if win else -0.01))
        decisions.append(replace(sim.decisions[0], game_id=1000 + i, betting_day=d, day_start_bankroll=1.0, wagers=ws))
    return decisions, day_ledgers(decisions)


def test_bootstrap_resamples_days_not_bets():
    decisions, days = _synthetic_days(40, hedge=True)
    b = metrics.day_block_bootstrap(days, decisions, n_resamples=2000, seed=1)
    assert b["status"] == "ok" and b["unit"] == "betting_day" and b["n_days"] == 40
    assert b["yield_ci95"] == [pytest.approx(0.0, abs=1e-12)] * 2                    # 同日一起抽 → 無變異


def test_bootstrap_insufficient_sample():
    decisions, days = _synthetic_days(metrics.MIN_BOOTSTRAP_DAYS - 1, hedge=False)
    b = metrics.day_block_bootstrap(days, decisions)
    assert b["status"] == "insufficient_sample" and "yield_ci95" not in b and b["min_days"] == 30


def test_bootstrap_deterministic_seed():
    decisions, days = _synthetic_days(45, hedge=False)
    a = metrics.day_block_bootstrap(days, decisions, n_resamples=3000, seed=7)
    b = metrics.day_block_bootstrap(days, decisions, n_resamples=3000, seed=7)
    c = metrics.day_block_bootstrap(days, decisions, n_resamples=3000, seed=8)
    assert a == b and a["yield_ci95"] != c["yield_ci95"]
    assert a["yield_ci95"][0] < a["yield_point"] < a["yield_ci95"][1]


def test_subgroups_are_descriptive_only(sim):
    r = metrics.subgroup_report(sim.wagers)
    assert r["label"] == "exploratory_descriptive_only"
    assert set(r["groups"]) >= {"market", "source", "bookmaker", "month", "side", "ev_bin", "odds_range",
                                "season_phase"}
    assert sum(g["n_bets"] for g in r["groups"]["market"].values()) == 7


def test_engine_has_no_result_feedback_into_selection():
    """選擇 / sizing 程式碼不得讀取 metrics（ROI 不回饋到策略）。"""
    for name in ("asof.py", "engine.py", "policy.py"):
        tree = ast.parse((REPO / "pipeline" / "core" / "execution" / name).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "metrics" not in names and not any(m and m.endswith("metrics") for m in mods)


# ======================= evidence ======================= #

def test_evidence_classification():
    t = D1_T1
    snap = tw(MONEYLINE, prices={"home": 2.0, "away": 1.8})
    run_ok = {"id": 9, "source": "twsport", "fetched_at": t, "outcome": "success"}
    observed = odds_row(1, 101, snap, t, t, tag=None, fetch_run_id=9, content_hash="abc")
    assert evidence.classify_odds_row(observed, {9: run_ok}) == (evidence.OBSERVED, None)
    seed = odds_row(2, 101, snap, t, None, tag=None, content_hash=None, normalizer_version=None)
    assert evidence.classify_odds_row(seed, {})[0] == evidence.SEED_FIXTURE
    assert evidence.classify_odds_row(odds_row(3, 101, snap, t), {9: run_ok})[0] == evidence.SYNTHETIC_FIXTURE
    for bad_run in ({**run_ok, "source": "oddsapi"}, {**run_ok, "outcome": "blocked"},
                    {**run_ok, "fetched_at": t + timedelta(seconds=1)}):
        assert evidence.classify_odds_row(observed, {9: bad_run})[0] == evidence.UNVERIFIED
    assert evidence.classify_odds_row({**observed, "fetch_run_id": None}, {})[0] == evidence.UNVERIFIED
    keep, rejected = evidence.partition_odds_rows([observed, seed], [run_ok])
    assert [r["id"] for r in keep] == [1] and rejected == {"seed_fixture:legacy_or_seed_row": 1}


def test_fixture_marked_validation_only_never_historical():
    r = run_fixture_validation()
    e = r["evidence"]
    assert e["evidence_class"] == "fixture_validation" and e["validation_only"] is True
    assert e["historical_evidence_available"] is False and e["is_taiwan_sports_lottery_evidence"] is False
    assert "NOT historical" in e["statement"]


def test_historical_guard_rejects_non_observed_and_no_evidence():
    e = evidence.evidence_summary(scope=PRIMARY_SCOPE, validation_only=False, n_observed_rows=0)
    assert e["evidence_class"] == "none" and e["historical_evidence_available"] is False
    with pytest.raises(evidence.EvidenceError):
        evidence.evidence_summary(scope=PRIMARY_SCOPE, validation_only=False, n_observed_rows=1,
                                  used_snapshot_ids=[1, 2], observed_ids=[1])
    ok = evidence.evidence_summary(scope=PRIMARY_SCOPE, validation_only=False, n_observed_rows=1,
                                   used_snapshot_ids=[1], observed_ids=[1])
    assert ok["evidence_class"] == "historical_observed" and ok["is_taiwan_sports_lottery_evidence"] is True


def test_oddsapi_results_never_labelled_twsport():
    pin = parse_scope("oddsapi:pinnacle")
    for kw in (dict(n_observed_rows=1, used_snapshot_ids=[1], observed_ids=[1]), dict(prospective=True)):
        e = evidence.evidence_summary(scope=pin, validation_only=False, **kw)
        assert e["evidence_label"] == "international_market_diagnostic"
        assert e["is_taiwan_sports_lottery_evidence"] is False and "not Taiwan Sports Lottery" in e["statement"]


def test_prediction_provenance():
    p = pred_row(1, 101, D1_T1)
    assert not evidence.prediction_is_production(p)                                  # fixture 標記
    assert evidence.prediction_is_production(pred_row(1, 101, D1_T1, tag=False))
    assert not evidence.prediction_is_production({**pred_row(1, 101, D1_T1, tag=False), "model_version": "ml-v1.0"})


# ======================= scheduler ======================= #

def test_paper_job_scheduled_every_5_minutes_separately_from_pricing():
    from core.execution import ledger
    from core.scheduling import job_specs, next_run_times

    specs = {s.id: s for s in job_specs()}
    s = specs["paper_strategy"]
    assert s.func is ledger.paper_strategy_scheduled
    assert specs["market_pricing"].func.__name__ == "pricing_and_sizing_scheduled"   # D.2 / D.3 不受影響
    now = datetime(2026, 10, 21, 22, 0, 30, tzinfo=UTC)
    nxt = next_run_times([s], now)["paper_strategy"]
    assert nxt.astimezone(UTC) == datetime(2026, 10, 21, 22, 2, tzinfo=UTC)          # :02 → T=22:00 延遲 2 分
    gaps = []
    t = now
    for _ in range(4):
        n = s.trigger.get_next_fire_time(None, t)
        gaps.append(n)
        t = n + timedelta(seconds=1)
    assert all((b - a) == timedelta(minutes=5) for a, b in zip(gaps, gaps[1:]))
    assert EXECUTION_V1.max_evaluation_lag >= timedelta(minutes=10)                  # 至少兩次排程機會


def test_paper_scheduled_isolates_decision_failure_from_settlement(monkeypatch):
    from contextlib import contextmanager

    from core import db as coredb
    from core.execution import ledger

    calls, beats = [], []

    def boom(now):
        calls.append("decide")
        raise RuntimeError("decision failed")

    monkeypatch.setattr(ledger, "paper_decision_job", boom)
    monkeypatch.setattr(ledger, "paper_settlement_job", lambda now: calls.append("settle"))

    @contextmanager
    def fake_cursor():
        yield None
    monkeypatch.setattr(coredb, "cursor", fake_cursor)
    monkeypatch.setattr(coredb, "heartbeat", lambda cur, **kw: beats.append(kw))
    ledger.paper_strategy_scheduled()
    assert calls == ["decide", "settle"]
    assert beats[0]["status"] == "error" and "decision failed" in beats[0]["error"]
    assert beats[0]["source_key"] == "paper_strategy"
