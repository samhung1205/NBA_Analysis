"""Phase D.5：actual-bet exposure / rescale / already-recorded / bankroll ledger / evidence / board states / 無 TS 數學
（純邏輯，不連 DB）。數字皆為手算可驗證的精確值。"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.decision import bankroll as bk
from core.decision import board, evidence as ev, exposure as ex, policy as P
from core.execution import settlement as settle_v1
from core.execution.engine import GameDecision, Wager
from core.pricing import engine as pricing_engine
from core.sizing import engine as sz
from core.sizing.policy import RISK_V1

UTC = timezone.utc
T = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)            # 物化時點（台灣 10/22 04:00）
TIP = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)         # 開賽（台灣 10/22 07:30）→ betting day 2026-10-22
DAY = date(2026, 10, 22)
NEXT_TIP = TIP + timedelta(days=1)
ART = "ml-v2.0+test"
REPO = Path(__file__).resolve().parents[2]


# ======================= fixtures（DB 形狀的列） ======================= #

def game(gid, start=TIP, status="scheduled"):
    return {"id": gid, "date_utc": start, "status": status}


def pred(pid, gid, created=T - timedelta(hours=2)):
    return {"id": pid, "game_id": gid, "model_version": "ml-v2.0", "created_at": created, "home_win_prob": 0.6,
            "pred_margin": 4.0, "pred_total": 224.0, "pred_home_h1": 57.0, "pred_away_h1": 55.0,
            "features_json": {"artifact_version": ART, "profile": "final", "prediction_kind": "final",
                              "prediction_as_of_utc": created.isoformat(), "data_quality": {"flags": []}}}


def odds_ml(oid, gid, home, away, *, source="twsport", book="twsport", fetched=T - timedelta(minutes=10),
            seen=T - timedelta(minutes=5), status="open"):
    return {"id": oid, "game_id": gid, "source": source, "bookmaker": book, "market": "ml", "market_type": "moneyline",
            "period": "full_game", "outcome_set": "two_way", "model_threshold": 0.0, "market_status": status,
            "fetched_at": fetched, "last_seen_at": seen, "home_odds": home, "away_odds": away, "line": None}


def odds_total(oid, gid, line, over, under, *, source="twsport", book="twsport", fetched=T - timedelta(minutes=10),
               seen=T - timedelta(minutes=5)):
    return {"id": oid, "game_id": gid, "source": source, "bookmaker": book, "market": "total", "market_type": "total",
            "period": "full_game", "outcome_set": "two_way", "model_threshold": line, "market_status": "open",
            "fetched_at": fetched, "last_seen_at": seen, "over_odds": over, "under_odds": under, "line": line}


def pricing_for(orow, pid, probs, *, base_id, flags=()):
    """probs：side → (p_win, p_push, p_loss)。EV 用 D.2 定義計算（與 pricing engine 寫入的相同）。"""
    sides = ("home", "away") if orow["market"] == "ml" else ("over", "under")
    odds = {s: orow[f"{s}_odds"] for s in sides}
    raw = {s: 1 / odds[s] for s in sides}
    tot = sum(raw.values())
    out = []
    for i, s in enumerate(sides):
        pw, pp, pl = probs[s]
        out.append({"id": base_id + i, "pricing_version": "pricing-v1", "odds_snapshot_id": orow["id"],
                    "prediction_id": pid, "game_id": orow["game_id"], "analysis_as_of": orow["fetched_at"],
                    "odds_fetched_at": orow["fetched_at"], "status": "priced",
                    "settlement_rule": "moneyline_ot_included" if orow["market"] == "ml" else "half_line_no_push",
                    "source": orow["source"], "bookmaker": orow["bookmaker"], "market": orow["market"],
                    "market_type": orow["market_type"], "period": "full_game", "outcome_set": "two_way",
                    "line": orow.get("line"), "side": s, "display_line": orow.get("line"), "decimal_odds": odds[s],
                    "raw_implied_prob": raw[s], "fair_no_vig_prob": raw[s] / tot, "market_overround": tot - 1,
                    "model_prob": pw, "push_prob": pp, "loss_prob": pl,
                    "ev_per_unit": pricing_engine.expected_value(pw, pp, pl, odds[s]),
                    "edge_vs_fair": pw - raw[s] / tot, "artifact_version": ART,
                    "model_target": "margin" if orow["market"] == "ml" else "total",
                    "model_threshold": orow["model_threshold"], "comparator": "gt" if s in ("home", "over") else "lt",
                    "warnings": "[]", "diagnostics": {"data_quality_flags": list(flags)}})
    return out


def slate(n_games=4, *, start=TIP, gid0=1, flags=()):
    """每場：ML 主 2.00（p .60 → ¼K 5% → 2%）+ 大分 1.95（p .58 → 2%）。同場 4% → 3%；4 場 12% → 8%。"""
    games, odds, preds, pricing = [], [], [], []
    for k in range(n_games):
        g = gid0 + k
        games.append(game(g, start))
        preds.append(pred(100 + g, g))
        o1, o2 = odds_ml(1000 + g * 10, g, 2.0, 1.9), odds_total(1000 + g * 10 + 1, g, 220.5, 1.95, 1.9)
        odds += [o1, o2]
        pricing += pricing_for(o1, 100 + g, {"home": (0.6, 0.0, 0.4), "away": (0.4, 0.0, 0.6)}, base_id=g * 100,
                               flags=flags)
        pricing += pricing_for(o2, 100 + g, {"over": (0.58, 0.0, 0.42), "under": (0.42, 0.0, 0.58)},
                               base_id=g * 100 + 10, flags=flags)
    return games, odds, preds, pricing


ACCOUNT = {"id": 1, "user_id": 1, "currency": "TWD", "risk_state_version": 3}
FUNDING = [bk.LedgerEntry(1, "initial_funding", 10000.0, T - timedelta(days=3))]
TWS_OK = {"source_key": "twsport", "last_status": "ok", "last_outcome": "success",
          "last_success_at": T - timedelta(minutes=10)}


def bet(bid, gid, stake, *, market="spread", side="home", day=DAY, result="pending", origin="manual_unlinked",
        recorded=T - timedelta(hours=1), status=P.ACTIVE, odds=1.9, voided_at=None):
    return ex.ActualBet(bet_id=bid, game_id=gid, betting_day=day, market=market, side=side, stake=stake, odds=odds,
                        record_status=status, result=result, recorded_time=recorded, origin=origin,
                        strategy_compliance="manual_unlinked" if origin else None, voided_at=voided_at)


def build(bets=(), *, n_games=4, account=ACCOUNT, ledger=FUNDING, day_snapshot=None, extra_games=(),
          slate_kw=None, evidence=None, tw_source=TWS_OK, odds_extra=(), pricing_extra=(), now=T, day=DAY):
    games, odds, preds, pricing = slate(n_games, **(slate_kw or {}))
    games += list(extra_games)
    return board.build_board(board.BoardInputs(
        now=now, betting_day=day, user_id=1, games=games, odds_rows=odds + list(odds_extra), pred_rows=preds,
        pricing_rows=pricing + list(pricing_extra), bets=list(bets), account=account, ledger=list(ledger),
        day_snapshot=day_snapshot, twsport_source=tw_source, paper={}, evidence=evidence or {"badge": "x"}))


def tw(res, status=None):
    return [o for o in res.opportunities if o["scope_kind"] == "taiwan_primary"
            and (status is None or o["decision_status"] == status)]


def q(res):
    return tw(res, P.QUALIFIED)


def d3_taiwan_final(n_games=4):
    """同一組台彩機會的 D.3 static final（沒有任何實際下注）。"""
    from core.sizing.job import select_candidates
    games, odds, preds, pricing = slate(n_games)
    sel = select_candidates(games, odds, preds, pricing, T)
    return {r.market_pricing_snapshot_id: r for r in sz.size_portfolio(sel.candidates, as_of=T)}


# ======================= §41 actual exposure ======================= #

def test_no_actual_bets_matches_theoretical_risk_v1_exactly():
    res = build()
    d3 = d3_taiwan_final()
    qs = q(res)
    assert len(qs) == 8 and res.snapshot["status"] == "ok"
    for o in qs:
        r = d3[o["pricing_snapshot_id"]]
        assert o["user_adjusted_fraction"] == r.final_stake_fraction == o["theoretical_final_fraction"]   # 逐位元相同
        assert o["single_bet_capped_fraction"] == 0.02
    assert math.fsum(o["user_adjusted_fraction"] for o in qs) == pytest.approx(0.08, abs=1e-15)
    # 每場 4% → 3%（× 0.75），當日 12% → 8%（× 2/3）：每筆 0.02 × 0.75 × 2/3 = 0.01
    assert all(o["user_adjusted_fraction"] == pytest.approx(0.01, abs=1e-15) for o in qs)
    assert all(o["max_additional_stake_amount"] == pytest.approx(100.0) and o["suggested_stake_amount"] == 100.0
               for o in qs)


def test_actual_daily_5pct_leaves_at_most_3pct():
    res = build([bet(1, 9, 500.0)], extra_games=[game(9)])                        # 另一場（無台彩盤口）已下 5%
    qs = q(res)
    assert res.snapshot["actual_exposure"]["day_fraction"] == pytest.approx(0.05)
    assert res.snapshot["actual_exposure"]["remaining_day_fraction"] == pytest.approx(0.03)
    assert math.fsum(o["user_adjusted_fraction"] for o in qs) == pytest.approx(0.03, abs=1e-12)
    assert all(o["day_scale_factor"] == pytest.approx(0.03 / 0.12) for o in qs)   # 等比例（不挑最高 EV）
    assert len({round(o["user_adjusted_fraction"], 15) for o in qs}) == 1


def test_actual_game_2pct_leaves_at_most_1pct_in_that_game():
    res = build([bet(1, 1, 200.0, market="spread")], n_games=1)                    # 同場其他玩法已下 2%
    qs = q(res)
    assert {o["market"] for o in qs} == {"ml", "total"}                             # spread 不是同一市場 → 不算 already_recorded
    assert math.fsum(o["user_adjusted_fraction"] for o in qs) == pytest.approx(0.01, abs=1e-12)
    assert all(o["actual_game_exposure"] == pytest.approx(0.02) and o["remaining_game_fraction"] == pytest.approx(0.01)
               for o in qs)


def test_actual_day_at_or_above_8pct_means_no_new_stake():
    res = build([bet(1, 9, 800.0)], extra_games=[game(9)])
    assert not q(res) and {o["decision_status"] for o in tw(res) if o["d3_qualification_status"] in
                           ex.PARTICIPANT_STATUSES} == {P.RISK_CAP_REACHED}
    over = build([bet(1, 9, 1000.0)], extra_games=[game(9)])
    exp = over.snapshot["actual_exposure"]
    assert exp["day_fraction"] == pytest.approx(0.10) and exp["remaining_day_fraction"] == 0.0 and exp["day_over_limit"]
    assert over.snapshot["status"] == P.ACTUAL_EXPOSURE_OVER_LIMIT
    for o in tw(over):
        if o["d3_qualification_status"] in ex.PARTICIPANT_STATUSES:
            assert o["decision_status"] == P.ACTUAL_EXPOSURE_OVER_LIMIT and o["user_adjusted_fraction"] == 0.0
            assert o["remaining_day_fraction"] == 0.0 and o["max_additional_stake_amount"] is None   # 不用負數額度


def test_actual_game_at_or_above_3pct_blocks_game():
    res = build([bet(1, 1, 300.0)], n_games=2)
    g1 = [o for o in tw(res) if o["game_id"] == 1 and o["d3_qualification_status"] in ex.PARTICIPANT_STATUSES]
    assert g1 and all(o["decision_status"] == P.RISK_CAP_REACHED for o in g1)
    assert all(o["user_adjusted_fraction"] > 0 for o in q(res)) and {o["game_id"] for o in q(res)} == {2}
    over = build([bet(1, 1, 400.0)], n_games=2)
    assert {o["decision_status"] for o in tw(over) if o["game_id"] == 1 and o["d3_qualification_status"]
            in ex.PARTICIPANT_STATUSES} == {P.ACTUAL_EXPOSURE_OVER_LIMIT}
    assert over.snapshot["actual_exposure"]["games_over_limit"] == [1]


def test_settled_same_day_bet_still_consumes_budget():
    pend = build([bet(1, 9, 500.0)], extra_games=[game(9)])
    won = build([bet(1, 9, 500.0, result="win")], extra_games=[game(9)])
    lost = build([bet(1, 9, 500.0, result="lose")], extra_games=[game(9)])
    for r in (won, lost):
        assert r.snapshot["actual_exposure"]["day_fraction"] == pend.snapshot["actual_exposure"]["day_fraction"]
        assert [o["user_adjusted_fraction"] for o in q(r)] == [o["user_adjusted_fraction"] for o in q(pend)]


def test_next_betting_day_resets_daily_exposure():
    nxt = DAY + timedelta(days=1)
    res = build([bet(1, 9, 800.0)], extra_games=[game(9)], slate_kw={"start": NEXT_TIP}, day=nxt)
    assert res.snapshot["actual_exposure"]["day_fraction"] == 0.0
    assert math.fsum(o["user_adjusted_fraction"] for o in q(res)) == pytest.approx(0.08)


def _frozen(start=10000.0):
    return {"id": 7, "day_start_bankroll": start, "basis_as_of": T - timedelta(hours=2), "ledger_balance": start,
            "open_stake_excluded": 0.0, "ledger_watermark": 1, "established_reason": "first_recorded_bet",
            "bankroll_version": P.BANKROLL_VERSION}


def test_stake_fraction_uses_frozen_day_start_and_midday_changes_do_not_alter_basis():
    ledger = FUNDING + [bk.LedgerEntry(2, "deposit", 10000.0, T - timedelta(minutes=30)),
                        bk.LedgerEntry(3, "bet_settlement", 900.0, T - timedelta(minutes=20), bet_id=5,
                                       settlement_key="bet:5:0:win")]
    res = build([bet(1, 9, 500.0)], extra_games=[game(9)], ledger=ledger, day_snapshot=_frozen())
    assert res.new_day_start is None                                                # 已凍結：不重算
    assert res.snapshot["actual_exposure"]["day_start_bankroll"] == 10000.0
    assert res.snapshot["actual_exposure"]["day_fraction"] == pytest.approx(0.05)    # 500 / 10000（不是 / 20900）
    assert res.bet_fractions == {1: pytest.approx(0.05)}
    assert res.snapshot["bankroll"]["current_bankroll"] == pytest.approx(20900.0)
    assert all(o["max_additional_stake_amount"] == pytest.approx(o["user_adjusted_fraction"] * 10000.0) for o in q(res))


def test_day_start_frozen_on_first_need_with_basis_at_first_bet():
    ledger = FUNDING + [bk.LedgerEntry(2, "deposit", 5000.0, T - timedelta(minutes=30))]
    res = build([bet(1, 9, 500.0, recorded=T - timedelta(hours=1))], extra_games=[game(9)], ledger=ledger)
    d = res.new_day_start
    assert d.basis_as_of == T - timedelta(hours=1) and d.established_reason == "first_recorded_bet"
    assert d.day_start_bankroll == 10000.0                                          # 之後的存入不回填當日基準
    no_bets = build(ledger=ledger)
    assert no_bets.new_day_start.basis_as_of == T and no_bets.new_day_start.day_start_bankroll == 15000.0


def test_legacy_unlinked_bet_counted_with_warning():
    res = build([bet(1, 9, 300.0, origin=None)], extra_games=[game(9)])
    exp = res.snapshot["actual_exposure"]
    assert exp["day_fraction"] == pytest.approx(0.03) and "bet:1:legacy_unlinked_bet" in exp["warnings"]
    assert exp["bets"][0]["legacy"] is True and exp["bets"][0]["counted"] is True
    assert math.fsum(o["user_adjusted_fraction"] for o in q(res)) == pytest.approx(0.05)


def test_manual_unlinked_bet_counted_in_exposure():
    res = build([bet(1, 2, 150.0, market="spread", origin="manual_unlinked")], n_games=2)
    assert res.snapshot["actual_exposure"]["game_fraction"] == {"2": pytest.approx(0.015)}
    assert all(o["remaining_game_fraction"] == pytest.approx(0.015) for o in q(res) if o["game_id"] == 2)


def test_incomplete_bet_data_is_conservative():
    res = build([bet(1, 9, None)], extra_games=[game(9)])
    assert res.snapshot["status"] == "actual_exposure_incomplete"
    assert "actual_exposure_may_be_incomplete" in res.snapshot["warnings"]
    assert not q(res) and {o["decision_status"] for o in tw(res) if o["d3_qualification_status"]
                           in ex.PARTICIPANT_STATUSES} == {P.DATA_INCOMPLETE}
    unknown_day = build([replace(bet(1, None, 100.0), betting_day=None)])
    assert unknown_day.snapshot["status"] == "actual_exposure_incomplete" and not q(unknown_day)


def test_bet_without_game_counts_against_every_game():
    e = ex.actual_exposure([bet(1, None, 100.0)], DAY, 10000.0)
    assert e.day_fraction == pytest.approx(0.01) and e.fraction_for_game(1) == pytest.approx(0.01)
    assert e.fraction_for_game(42) == pytest.approx(0.01)


def test_voided_and_superseded_bets_do_not_count():
    e = ex.actual_exposure([bet(1, 1, 500.0, status="voided"), bet(2, 1, 500.0, status="superseded"),
                            bet(3, 1, 100.0)], DAY, 10000.0)
    assert e.day_stake == 100.0 and e.day_fraction == pytest.approx(0.01)


def test_rescale_rejects_non_participants_and_requires_basis():
    r = sz.SizingResult(sizing_version="s", risk_policy_version="risk-v1", kelly_math_version="k",
                        market_pricing_snapshot_id=1, odds_snapshot_id=1, prediction_id=1, pricing_version="p",
                        game_id=1, betting_day=DAY, analysis_as_of=T, pricing_analysis_as_of=T, source="twsport",
                        bookmaker="twsport", market="ml", market_type="moneyline", period="full_game",
                        outcome_set="two_way", line=None, side="home", display_line=None, settlement_rule="x",
                        decimal_odds=2.0, p_win=0.4, p_push=0.0, p_loss=0.6, ev_per_unit=-0.2, edge_vs_fair=0.0,
                        kelly_multiplier=0.25, max_bet_fraction=0.02, max_game_fraction=0.03, max_day_fraction=0.08,
                        qualification_status=sz.NO_POSITIVE_EV, single_bet_capped_fraction=0.0)
    with pytest.raises(ValueError):
        ex.rescale_with_actual([r], ex.actual_exposure([], DAY, 10000.0))
    with pytest.raises(ValueError):
        ex.rescale_with_actual([], ex.actual_exposure([bet(1, 1, 10.0)], DAY, None))


# ======================= §42 linked opportunity ======================= #

def _ml_home(res, gid=1):
    return next(o for o in tw(res) if o["game_id"] == gid and o["market"] == "ml" and o["side"] == "home")


def test_linked_actual_bet_is_already_recorded_and_no_top_up():
    res = build([bet(1, 1, 50.0, market="ml", side="home", origin="platform_opportunity")], n_games=1)
    o = _ml_home(res)
    assert o["decision_status"] == P.ALREADY_RECORDED and o["status_group"] == P.RECORDED
    assert o["user_adjusted_fraction"] == 0.0 and o["max_additional_stake_amount"] is None
    assert o["linked_bet_ids"] == [1] and o["linked_actual_fraction"] == pytest.approx(0.005)
    assert "below_theoretical_target_no_top_up" in o["reasons"] and "no_top_up:decision-v1" in o["reasons"]
    away = next(x for x in tw(res) if x["market"] == "ml" and x["side"] == "away")
    assert away["decision_status"] == P.ALREADY_RECORDED                      # 同一市場另一邊也不提出新增額度
    total = [x for x in q(res) if x["market"] == "total"]
    # 同場剩 3% − 0.5% = 2.5%；大分 capped 2% ≤ 2.5% → 不縮放
    assert len(total) == 1 and total[0]["user_adjusted_fraction"] == pytest.approx(0.02)


def test_actual_stake_above_theoretical_gets_warning_not_cancellation():
    res = build([bet(1, 1, 250.0, market="ml", side="home")], n_games=1)
    o = _ml_home(res)
    assert o["decision_status"] == P.ALREADY_RECORDED and "over_theoretical_target" in o["warnings"]
    assert res.snapshot["actual_exposure"]["bets"][0]["stake"] == 250.0      # 不修改歷史 bet


def test_better_later_odds_do_not_create_duplicate_opportunity():
    newer = odds_ml(5000, 1, 2.5, 1.6, fetched=T - timedelta(minutes=2), seen=T - timedelta(minutes=1))
    pr = pricing_for(newer, 101, {"home": (0.6, 0.0, 0.4), "away": (0.4, 0.0, 0.6)}, base_id=50000)
    res = build([bet(1, 1, 100.0, market="ml", side="home")], n_games=1, odds_extra=[newer], pricing_extra=pr)
    o = _ml_home(res)
    assert o["decimal_odds"] == 2.5                                               # 顯示最新觀測賠率
    assert o["decision_status"] == P.ALREADY_RECORDED and o["user_adjusted_fraction"] == 0.0


# ======================= board states / Taiwan vs international ======================= #

def test_international_odds_never_become_taiwan_actionable():
    games, _, preds, _ = slate(1)
    o = odds_ml(1, 1, 2.2, 1.7, source="oddsapi", book="pinnacle")
    pr = pricing_for(o, 101, {"home": (0.6, 0.0, 0.4), "away": (0.4, 0.0, 0.6)}, base_id=1)
    res = board.build_board(board.BoardInputs(now=T, betting_day=DAY, user_id=1, games=games, odds_rows=[o],
                                              pred_rows=preds, pricing_rows=pr, account=ACCOUNT, ledger=FUNDING,
                                              twsport_source=TWS_OK, paper={}))
    assert not tw(res) and res.snapshot["summary"]["n_qualified"] == 0
    intl = [x for x in res.opportunities if x["scope_kind"] == "international_diagnostic"]
    assert len(intl) == 2 and all(x["status_group"] == P.DIAGNOSTIC and x["evidence_label"] ==
                                  "international_market_diagnostic" and x["user_adjusted_fraction"] is None
                                  and x["max_additional_stake_amount"] is None for x in intl)
    assert res.snapshot["games"][0]["taiwan_odds_state"] == P.TW_NOT_PUBLISHED
    assert res.snapshot["games"][0]["status_group"] == P.BLOCKED and res.snapshot["status"] == "no_taiwan_odds"


@pytest.mark.parametrize("source,expected", [
    (None, P.TW_NOT_CONFIGURED),
    ({"last_status": "warn", "last_outcome": "blocked", "last_success_at": None}, P.TW_INGESTION_UNAVAILABLE),
    ({"last_status": "ok", "last_outcome": "success", "last_success_at": T - timedelta(hours=5)},
     P.TW_INGESTION_UNAVAILABLE),
    ({"last_status": "ok", "last_outcome": "success", "last_success_at": T - timedelta(minutes=20)},
     P.TW_NOT_PUBLISHED)])
def test_missing_taiwan_odds_reasons(source, expected):
    state, reason = board.taiwan_odds_state(game(1), T, [], source)
    assert state == expected
    if expected == P.TW_INGESTION_UNAVAILABLE:
        assert reason.endswith("har_not_imported")


def test_taiwan_odds_stale_closed_window_started():
    stale = odds_ml(1, 1, 2.0, 1.9, fetched=T - timedelta(hours=3), seen=T - timedelta(hours=2))
    assert board.taiwan_odds_state(game(1), T, [stale], TWS_OK)[0] == P.TW_STALE
    closed = odds_ml(2, 1, 2.0, 1.9, status="suspended")
    assert board.taiwan_odds_state(game(1), T, [closed], TWS_OK)[0] == P.TW_MARKET_CLOSED
    far = game(1, T + timedelta(hours=72))
    assert board.taiwan_odds_state(far, T, [], TWS_OK)[0] == P.TW_OUTSIDE_WINDOW
    assert board.taiwan_odds_state(game(1, status="live"), T, [], TWS_OK)[0] == P.TW_GAME_STARTED


def test_stale_taiwan_quote_is_blocked_not_actionable():
    games, _, preds, _ = slate(1)
    o = odds_ml(1, 1, 2.0, 1.9, fetched=T - timedelta(hours=3), seen=T - timedelta(hours=2))
    pr = pricing_for(o, 101, {"home": (0.6, 0.0, 0.4), "away": (0.4, 0.0, 0.6)}, base_id=1)
    res = board.build_board(board.BoardInputs(now=T, betting_day=DAY, user_id=1, games=games, odds_rows=[o],
                                              pred_rows=preds, pricing_rows=pr, account=ACCOUNT, ledger=FUNDING,
                                              twsport_source=TWS_OK, paper={}))
    assert _ml_home(res)["decision_status"] == P.STALE_ODDS and not q(res)


def test_bankroll_unavailable_has_fractions_but_no_amounts():
    res = build(account=None, ledger=[])
    assert res.snapshot["status"] == P.BANKROLL_UNAVAILABLE and res.snapshot["bankroll"]["configured"] is False
    part = [o for o in tw(res) if o["d3_qualification_status"] in ex.PARTICIPANT_STATUSES]
    assert part and all(o["decision_status"] == P.BANKROLL_UNAVAILABLE and o["max_additional_stake_amount"] is None
                        for o in part)
    assert math.fsum(o["user_adjusted_fraction"] for o in part) == pytest.approx(0.08)   # 理論比例仍顯示
    with_bets = build([bet(1, 9, 100.0)], extra_games=[game(9)], account=None, ledger=[])
    assert all(o["user_adjusted_fraction"] is None for o in tw(with_bets)
               if o["decision_status"] == P.BANKROLL_UNAVAILABLE)                        # 有實際下注卻無基準：不猜


def test_data_quality_flags_are_review_not_stake_changes():
    plain = build(n_games=1)
    flagged = build(n_games=1, slate_kw={"flags": ("season_opener",)})
    assert {o["status_group"] for o in q(flagged)} == {P.REVIEW}
    assert [o["user_adjusted_fraction"] for o in q(flagged)] == [o["user_adjusted_fraction"] for o in q(plain)]


def test_no_minimum_ev_threshold_and_ranking_is_display_only():
    games, odds, preds, _ = slate(1)
    o = odds_ml(1, 1, 2.0, 1.9)
    pr = pricing_for(o, 101, {"home": (0.5005, 0.0, 0.4995), "away": (0.4995, 0.0, 0.5005)}, base_id=1)
    res = board.build_board(board.BoardInputs(now=T, betting_day=DAY, user_id=1, games=games, odds_rows=[o],
                                              pred_rows=preds, pricing_rows=pr, account=ACCOUNT, ledger=FUNDING,
                                              twsport_source=TWS_OK, paper={}))
    tiny = _ml_home(res)
    assert tiny["ev_per_unit"] == pytest.approx(0.001) and tiny["decision_status"] == P.QUALIFIED
    assert 0 < tiny["user_adjusted_fraction"] < 0.001
    a, b = build(), build()
    games, odds, preds, pricing = slate(4)
    rev = board.build_board(board.BoardInputs(now=T, betting_day=DAY, user_id=1, games=games[::-1],
                                              odds_rows=odds[::-1], pred_rows=preds[::-1], pricing_rows=pricing[::-1],
                                              account=ACCOUNT, ledger=FUNDING, twsport_source=TWS_OK, paper={},
                                              evidence={"badge": "x"}))
    key = lambda r: {o["pricing_snapshot_id"]: (o["user_adjusted_fraction"], o["display_rank"]) for o in r.opportunities}  # noqa: E731
    assert key(a) == key(b) == key(rev) and a.fingerprint == rev.fingerprint
    ranks = [o["display_rank"] for o in a.opportunities]
    assert ranks == sorted(ranks) and a.opportunities[0]["status_group"] == P.ACTIONABLE


def test_fingerprint_changes_with_bets_bankroll_and_version_only():
    a, b = build(), build()
    assert a.fingerprint == b.fingerprint
    assert build([bet(1, 9, 10.0)], extra_games=[game(9)]).fingerprint != a.fingerprint
    assert build(account={**ACCOUNT, "risk_state_version": 4}).fingerprint != a.fingerprint
    assert build(ledger=FUNDING + [bk.LedgerEntry(2, "deposit", 1.0, T - timedelta(minutes=1))]).fingerprint != a.fingerprint


# ======================= §43 bankroll ledger ======================= #

def L(i, t, amt, **kw):
    return bk.LedgerEntry(i, t, amt, T - timedelta(hours=10 - i), **kw)


def test_ledger_signed_amounts_and_audit_sum():
    es = [L(1, "initial_funding", 10000.0), L(2, "deposit", 500.0), L(3, "withdrawal", 200.0),
          L(4, "adjustment", -50.0, reason="fee"), L(5, "reversal", None, reverses_entry_id=2, reason="typo")]
    s = bk.signed_amounts(es)
    assert s == {1: 10000.0, 2: 500.0, 3: -200.0, 4: -50.0, 5: -500.0}
    assert bk.ledger_balance(es) == pytest.approx(9750.0)
    assert bk.ledger_balance(es, T - timedelta(hours=8)) == pytest.approx(10500.0)   # as-of（決定性）
    assert bk.ledger_watermark(es, T - timedelta(hours=8)) == 2


@pytest.mark.parametrize("es", [
    [L(1, "withdrawal", -5.0)], [L(1, "deposit", 0.0)], [L(1, "adjustment", 0.0, reason="x")],
    [L(1, "reversal", None, reverses_entry_id=2, reason="x"), L(2, "deposit", 5.0)],
    [L(1, "deposit", 5.0), L(2, "reversal", None, reverses_entry_id=1, reason="a"),
     L(3, "reversal", None, reverses_entry_id=1, reason="b")],
    [L(1, "bonus", 5.0)], [L(1, "deposit", float("nan"))]])
def test_ledger_rejects_invalid_entries(es):
    with pytest.raises(bk.LedgerError):
        bk.signed_amounts(es)


def test_settlement_ledger_actions_win_change_void_and_idempotency():
    b = bet(7, 1, 100.0, odds=1.9, result="win")
    acts = bk.settlement_ledger_actions([b], FUNDING)
    assert acts == [{"entry_type": "bet_settlement", "amount": pytest.approx(90.0), "bet_id": 7,
                     "settlement_key": "bet:7:0:win", "reason": "result:win"}]
    es = FUNDING + [bk.LedgerEntry(2, a["entry_type"], a["amount"], T, bet_id=7, settlement_key=a["settlement_key"])
                    for a in acts]
    assert bk.settlement_ledger_actions([b], es) == []                              # 冪等
    lost = replace(b, result="lose")
    acts2 = bk.settlement_ledger_actions([lost], es)
    assert [(a["entry_type"], a["amount"]) for a in acts2] == [("bet_settlement_reversal", pytest.approx(-90.0)),
                                                                ("bet_settlement", pytest.approx(-100.0))]
    es2 = es + [bk.LedgerEntry(3 + i, a["entry_type"], a["amount"], T, bet_id=7, settlement_key=a["settlement_key"])
                for i, a in enumerate(acts2)]
    assert bk.ledger_balance(es2) == pytest.approx(9900.0)                          # audit：初始 + 最終損益
    voided = replace(lost, record_status="voided")
    acts3 = bk.settlement_ledger_actions([voided], es2)
    assert [(a["entry_type"], a["amount"]) for a in acts3] == [("bet_settlement_reversal", pytest.approx(100.0))]
    push = bk.settlement_ledger_actions([bet(8, 1, 100.0, result="push")], FUNDING)
    assert push[0]["amount"] == 0.0 and push[0]["entry_type"] == "bet_settlement"   # push 也記錄（= 已結算）


def test_open_stake_and_day_start_exclude_other_days_open_bets():
    prev = bet(1, 5, 300.0, day=DAY - timedelta(days=1), recorded=T - timedelta(hours=20))
    today = bet(2, 1, 100.0, recorded=T - timedelta(hours=1))
    d = bk.compute_day_start(DAY, FUNDING, [prev, today], T - timedelta(hours=1), "first_recorded_bet")
    assert d.ledger_balance == 10000.0 and d.open_stake_excluded == 300.0 and d.day_start_bankroll == 9700.0
    settled = FUNDING + [bk.LedgerEntry(2, "bet_settlement", 270.0, T - timedelta(hours=2), bet_id=1,
                                        settlement_key="bet:1:0:win")]
    d2 = bk.compute_day_start(DAY, settled, [replace(prev, result="win"), today], T - timedelta(hours=1), "x")
    assert d2.day_start_bankroll == pytest.approx(10270.0) and d2.open_stake_excluded == 0.0
    assert bk.compute_day_start(DAY, [], [today], T, "x") is None                   # 沒有資金：不猜


def test_normalized_fractions_unaffected_by_currency_amount():
    small = build([bet(1, 9, 500.0)], extra_games=[game(9)])
    big = build([bet(1, 9, 500000.0)], extra_games=[game(9)],
                ledger=[bk.LedgerEntry(1, "initial_funding", 10_000_000.0, T - timedelta(days=3))])
    fr = lambda r: [(o["pricing_snapshot_id"], o["user_adjusted_fraction"], o["decision_status"]) for o in tw(r)]  # noqa: E731
    assert fr(small) == fr(big)
    assert all(b_["max_additional_stake_amount"] == pytest.approx(1000 * s["max_additional_stake_amount"])
               for s, b_ in zip(q(small), q(big)))


def test_bankroll_summary_views():
    es = FUNDING + [bk.LedgerEntry(2, "deposit", 1000.0, T - timedelta(hours=1))]
    bs = [bet(1, 1, 300.0), bet(2, 1, 200.0, result="win")]
    d = bk.DayStart(DAY, 10000.0, T - timedelta(hours=2), 10000.0, 0.0, 1, "first_recorded_bet")
    s = bk.bankroll_summary(es, bs, T, day_start=d, day_used_fraction=0.05, currency="TWD")
    assert s["current_bankroll"] == 11000.0 and s["committed_open_stake"] == 500.0          # win 尚未入帳 → 仍 open
    assert s["available_bankroll"] == 10500.0 and s["remaining_risk_budget_fraction"] == pytest.approx(0.03)
    assert s["remaining_risk_budget_amount"] == pytest.approx(300.0) and s["day_start_bankroll"] == 10000.0


# ======================= §45 evidence / paper vs actual ======================= #

def _paper_decisions(n_days):
    out = []
    for k in range(n_days):
        d = DAY + timedelta(days=k)
        t = datetime.combine(d, datetime.min.time(), tzinfo=UTC)
        w = Wager(strategy_id="s", game_id=k, betting_day=d, decision_time=t, scheduled_tipoff=t, odds_snapshot_id=k,
                  prediction_id=1, source="twsport", bookmaker="twsport", market="ml", market_type="moneyline",
                  period="full_game", outcome_set="two_way", side="home", line=None, display_line=None,
                  model_target="margin", model_threshold=0.0, comparator="gt", settlement_rule="moneyline_ot_included",
                  decimal_odds=2.0, p_win=0.55, p_push=0.0, p_loss=0.45, ev_per_unit=0.1,
                  sizing_final_stake_fraction=0.01, execution_scale_factor=1.0, stake_fraction=0.01, stake_units=0.01,
                  expected_profit_units=0.001, settlement_status=settle_v1.SETTLED_WIN if k % 2 else settle_v1.SETTLED_LOSS,
                  profit_units=0.01 if k % 2 else -0.01)
        out.append(GameDecision(strategy_id="s", game_id=k, betting_day=d, scheduled_tipoff=t, decision_time=t,
                                evaluated_at=t, decision_status="bet", no_bet_reason=None, blockers={},
                                odds_snapshot_ids=[k], prediction_id=1, prediction_available_at=t,
                                artifact_version=ART, model_version="ml-v2.0", distribution_version="dist-v2",
                                pricing_fingerprint="p", sizing_fingerprint="s", sizing=[], day_start_bankroll=1.0,
                                committed_fraction_before=0.0, remaining_day_fraction_before=0.08,
                                execution_scale_factor=1.0, total_stake_fraction=0.01, total_stake_units=0.01,
                                wagers=[w]))
    return out


def test_evidence_no_historical_betting_and_bootstrap_threshold():
    panel = ev.evidence_panel(ev.paper_evidence([], strategy_id="s"))
    assert panel["historical_betting_evidence"]["status"] == "unavailable"
    assert panel["historical_model_evidence"]["status"] == "available" and panel["affects_strategy"] is False
    assert panel["prospective_paper"]["bootstrap"]["status"] == "insufficient_sample"
    assert panel["badge"] == "prospective_validation"
    p29 = ev.paper_evidence(_paper_decisions(29), strategy_id="s")
    assert p29["n_graded_betting_days"] == 29 and p29["bootstrap"]["status"] == "insufficient_sample"
    assert "yield_ci95" not in p29["bootstrap"]
    p30 = ev.paper_evidence(_paper_decisions(30), strategy_id="s", n_resamples=500)
    assert p30["bootstrap"]["status"] == "ok" and len(p30["bootstrap"]["yield_ci95"]) == 2
    assert ev.paper_evidence(None)["status"] == "unavailable"


def test_evidence_state_does_not_alter_selection_or_stake():
    a = build(evidence=ev.evidence_panel(ev.paper_evidence([], strategy_id="s")))
    b = build(evidence=ev.evidence_panel(ev.paper_evidence(_paper_decisions(30), strategy_id="s", n_resamples=200)))
    assert a.opportunities == b.opportunities


def test_paper_and_actual_records_are_separate():
    res = build([bet(1, 9, 100.0, result="win")], extra_games=[game(9)])
    perf = res.snapshot["actual_performance"]
    assert perf["label"] == "user_actual_betting_record" and "NOT model strategy" in perf["statement"]
    assert perf["composition_by_origin"] == {"manual_unlinked": 1} and perf["includes_non_strategy_bets"] is True
    assert "paper" not in perf and res.snapshot["evidence"] == {"badge": "x"}


def test_paper_decision_attached_for_reference_only():
    games, odds, preds, pricing = slate(1)
    paper = {1: {"decision_id": 55, "decision_status": "bet", "wagers": [{"market": "ml", "side": "home"}]}}
    res = board.build_board(board.BoardInputs(now=T, betting_day=DAY, user_id=1, games=games, odds_rows=odds,
                                              pred_rows=preds, pricing_rows=pricing, account=ACCOUNT, ledger=FUNDING,
                                              twsport_source=TWS_OK, paper=paper))
    assert _ml_home(res)["paper_decision_id"] == 55 and res.snapshot["games"][0]["paper_decision"]["decision_id"] == 55
    assert [o["user_adjusted_fraction"] for o in q(res)] == [o["user_adjusted_fraction"] for o in q(build(n_games=1))]


# ======================= policy / no TS math ======================= #

def test_decision_policy_binds_frozen_risk_v1_without_new_version():
    assert P.RISK_POLICY is RISK_V1 and P.RISK_POLICY.to_dict() == RISK_V1.to_dict()
    assert (RISK_V1.kelly_multiplier, RISK_V1.max_bet_fraction, RISK_V1.max_game_fraction,
            RISK_V1.max_day_fraction) == (0.25, 0.02, 0.03, 0.08)
    assert P.TOP_UP_ALLOWED is False and P.MIN_EV_THRESHOLD is None and P.PRIMARY.source == "twsport"
    src = "\n".join(p.read_text() for p in (REPO / "pipeline" / "core" / "decision").glob("*.py"))
    assert "RiskPolicy(" not in src and "replace(RISK" not in src                 # 不建立新的 risk policy（無 risk-v1.1）
    assert "os.environ" not in src and "getenv" not in src


_WORDS = re.compile(r"kelly|stake_fraction|stake_amount|user_adjusted|remaining_|capacity|exposure|bankroll|day_start|"
                    r"max_additional|suggested_stake|ev_per_unit|max_(bet|game|day)", re.I)
_ARITH = re.compile(r"[A-Za-z0-9_)\]]\s*[*/]\s*[A-Za-z0-9_(.]|[A-Za-z0-9_)\]]\s+[-+]\s+[A-Za-z0-9_(.]"
                    r"|Math\.(min|max|floor|round)\s*\(")
_PERCENT_DISPLAY = re.compile(r"\*\s*100\b")          # 比例 → 百分比顯示（格式化，不是數學）


def _is_math(frag: str) -> bool:
    return bool(_WORDS.search(frag) and _ARITH.search(_PERCENT_DISPLAY.sub("", frag)))


def _code_fragments(text: str) -> list[tuple[int, str]]:
    """與 test_sizing 相同：去掉註解與字串內容，保留樣板字串的 ${…}（HTML 文字不檢查）。"""
    out = []
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
    for n, line in enumerate(text.splitlines(), 1):
        line = re.sub(r"(^|\s)//.*$", "", line)
        exprs = re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", line)
        code = re.sub(r"`[^`]*`|'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"", "''", line)
        if "<" in code and ">" in code:
            code = ""
        out.extend((n, frag) for frag in [code, *exprs] if frag.strip())
    return out


def test_no_exposure_capacity_or_bankroll_math_in_typescript_or_frontend():
    """D.5：exposure / remaining capacity / bankroll / Kelly / 縮放只在 Python。TS / 前端只查詢、序列化、比較 Python 物化的上限、格式化。"""
    files = sorted(set((REPO / "src").rglob("*.ts")) | set((REPO / "src").rglob("*.tsx")) |
                   set((REPO / "public" / "static" / "js").glob("*.js")))
    bad = [f"{f.relative_to(REPO)}:{n}: {frag.strip()}" for f in files for n, frag in _code_fragments(f.read_text())
           if _is_math(frag)]
    assert bad == [], "\n".join(bad)
    sample = ("const rem = 0.08 - exposure\nconst x = Math.min(remaining_game, remaining_day)\n"
              "html = `<td>${o.user_adjusted_fraction * bankroll}</td>`")
    assert len([1 for _, f in _code_fragments(sample) if _is_math(f)]) == 3
    assert not _is_math("NBA.signed(o.ev_per_unit * 100, 1) + ''") and not _is_math("push-aware full Kelly")
