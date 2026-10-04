"""Phase D.3：Kelly 數學 / risk-v1 policy / qualification / 互斥 / game & day exposure / 時間與新鮮度（純邏輯，不連 DB）

機率用手算得出的精確數字（不是 snapshot）；production 機率來源的正確性已在 D.2 / C.5E 測試。
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from scipy.optimize import minimize_scalar

from core.odds.canonical import FULL_GAME, H1, MONEYLINE, SPREAD, THREE_WAY, TOTAL, make_snapshot
from core.pricing import engine as pricing_engine
from core.pricing.engine import OddsInput, PredictionInput, price_market
from core.sizing import engine, kelly, policy
from core.sizing.engine import (ELIGIBLE, EXPOSURE_SCALED, INVALID_PROBABILITY, MARKET_NOT_OPEN, MUTUALLY_EXCLUSIVE,
                                NO_POSITIVE_EV, NO_PREDICTION, STALE_QUOTE, UNAVAILABLE, UNSUPPORTED_SETTLEMENT,
                                SizingCandidate, size_portfolio)
from core.sizing.job import select_candidates, size_pricings
from core.sizing.policy import RISK_V1, RiskPolicy

UTC = timezone.utc
T = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)            # sizing 時點（台灣 10/22 04:00）
TIP = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)         # 開賽（台灣 10/22 07:30）
REPO = Path(__file__).resolve().parents[2]


def cand(p_win, p_push, p_loss, odds, *, side="home", game_id=7, snap=None, pid=None, book="pinnacle",
         source="oddsapi", market="spread", rule=None, status="priced", start=TIP, fetched=None, last_seen=None,
         ev=None, **kw):
    """一個已定價 outcome。ev 預設由 D.2 定義計算（與 pricing engine 存的值相同）。"""
    if rule is None:
        rule = "integer_line_push_refund" if p_push else ("moneyline_ot_included" if market == "ml" else "half_line_no_push")
    if ev is None and None not in (p_win, p_push, p_loss, odds):
        ev = pricing_engine.expected_value(p_win, p_push, p_loss, odds)
    snap = snap if snap is not None else hash((game_id, book, market)) % 10_000
    fetched = fetched or min(T - timedelta(minutes=10), last_seen or T)   # last_seen_at ≥ fetched_at（D.1）
    base = dict(game_id=game_id, game_start_utc=start, side=side, pricing_status=status,
                pricing_analysis_as_of=fetched, odds_fetched_at=fetched, market_pricing_snapshot_id=pid,
                odds_snapshot_id=snap, prediction_id=1, pricing_version="pricing-v1", source=source, bookmaker=book,
                market=market, market_type={"ml": "moneyline", "spread": "spread", "total": "total"}.get(market, market),
                period=FULL_GAME, outcome_set="two_way", line=-5.5 if market == "spread" else None,
                display_line=None, settlement_rule=rule, decimal_odds=odds, p_win=p_win, p_push=p_push, p_loss=p_loss,
                ev_per_unit=ev, edge_vs_fair=0.0, fair_no_vig_prob=0.5, odds_last_seen_at=last_seen or fetched,
                artifact_version="ml-v2.0+test")
    base.update(kw)
    return SizingCandidate(**base)


def one(c, **kw):
    [r] = size_portfolio([c], as_of=kw.pop("as_of", T), **kw)
    return r


def pair(p_home, p_push, odds_home, odds_away, **kw):
    """同一 snapshot 的兩邊（機率一致：away 的 win = home 的 loss）。"""
    p_away = 1.0 - p_home - p_push
    return [cand(p_home, p_push, p_away, odds_home, side="home", **kw),
            cand(p_away, p_push, p_home, odds_away, side="away", **kw)]


# ======================= Kelly math ======================= #

def test_standard_no_push_kelly_exact_example_a():
    """Example A：1.91、p 0.55 / 0.45 → b 0.91、full = (0.55·0.91 − 0.45)/0.91 ≈ 0.0555、¼ ≈ 0.0139 < 2%。"""
    b = 1.91 - 1
    f = kelly.full_kelly_fraction(0.55, 0.0, 0.45, 1.91)
    assert f == pytest.approx((0.55 * b - 0.45) / b, abs=1e-15)
    assert f == pytest.approx(0.0554945054945055, abs=1e-15) and round(f, 4) == 0.0555
    r = one(cand(0.55, 0.0, 0.45, 1.91))
    assert r.qualification_status == ELIGIBLE and r.actionable
    assert r.fractional_kelly_fraction == pytest.approx(0.25 * f, abs=1e-15) and round(r.fractional_kelly_fraction, 4) == 0.0139
    assert r.single_bet_capped_fraction == r.fractional_kelly_fraction == r.final_stake_fraction    # < 2%，不受上限影響


def test_push_aware_kelly_exact_analytic_solution():
    """f* = (p_w·b − p_l) / (b·(p_w + p_l)) = EV / (b·(1 − p_push))。"""
    pw, pp, pl, d = 0.52, 0.05, 0.43, 1.91
    b = d - 1
    ev = pw * b - pl
    f = kelly.full_kelly_fraction(pw, pp, pl, d)
    assert f == pytest.approx((pw * b - pl) / (b * (pw + pl)), abs=1e-15)
    assert f == pytest.approx(ev / (b * (1 - pp)), abs=1e-15)
    assert ev == pytest.approx(0.0432, abs=1e-12) and f == pytest.approx(0.0432 / 0.8645, abs=1e-12)
    # 導數在 f* 為 0
    g_prime = pw * b / (1 + b * f) - pl / (1 - f)
    assert g_prime == pytest.approx(0.0, abs=1e-12)
    # 與錯誤做法不同：把 push 當輸、或用 1 − p_win 當 p_loss
    assert f != pytest.approx((pw * b - (pl + pp)) / b) and f != pytest.approx((pw * b - (1 - pw)) / b)


def test_example_c_positive_ev_with_push_exact_and_numerical():
    """Example C：賠率 2.00（b = 1）、p_win 0.50、p_push 0.10、p_loss 0.40 → EV 0.10、f* = 0.10/(1·0.9) = 1/9。"""
    f = kelly.full_kelly_fraction(0.5, 0.1, 0.4, 2.0)
    assert kelly.expected_value(0.5, 0.1, 0.4, 2.0) == pytest.approx(0.10, abs=1e-15)
    assert f == pytest.approx(1 / 9, abs=1e-15)
    res = minimize_scalar(lambda x: -kelly.expected_log_growth(x, 0.5, 0.1, 0.4, 2.0), bounds=(0, 0.999),
                          method="bounded", options={"xatol": 1e-12})
    assert res.x == pytest.approx(1 / 9, abs=1e-7)
    r = one(cand(0.5, 0.1, 0.4, 2.0))
    assert r.fractional_kelly_fraction == pytest.approx(1 / 36, abs=1e-15)                      # 0.02778 > 2%
    assert r.single_bet_capped_fraction == 0.02 and "single_bet_cap_applied" in r.reasons


@pytest.mark.parametrize("pw,pp,pl,d", [(0.55, 0.0, 0.45, 1.91), (0.52, 0.05, 0.43, 1.91), (0.5, 0.1, 0.4, 2.0),
                                        (0.3, 0.0, 0.7, 3.6), (0.035, 0.0, 0.965, 35.0), (0.49, 0.03, 0.48, 2.10),
                                        (0.62, 0.0, 0.38, 1.70), (0.2, 0.7, 0.1, 1.80)])
def test_numerical_expected_log_maximization_matches_analytic(pw, pp, pl, d):
    f = kelly.full_kelly_fraction(pw, pp, pl, d)
    res = minimize_scalar(lambda x: -kelly.expected_log_growth(x, pw, pp, pl, d), bounds=(0, 0.999999),
                          method="bounded", options={"xatol": 1e-12})
    numeric = max(res.x, 0.0) if kelly.expected_value(pw, pp, pl, d) > 0 else 0.0
    assert f == pytest.approx(numeric, abs=1e-6)
    if f > 0:                                                              # 解析解的成長不低於鄰近點
        g = kelly.expected_log_growth(f, pw, pp, pl, d)
        assert g >= kelly.expected_log_growth(f * 0.99, pw, pp, pl, d)
        assert g >= kelly.expected_log_growth(min(f * 1.01, 0.999999), pw, pp, pl, d)


def test_example_b_integer_push_negative_ev_gives_zero_kelly():
    """Example B：1.91、0.47 / 0.06 / 0.47 → EV = −0.0423 → Kelly 0。"""
    assert kelly.expected_value(0.47, 0.06, 0.47, 1.91) == pytest.approx(-0.0423, abs=1e-12)
    assert kelly.full_kelly_fraction(0.47, 0.06, 0.47, 1.91) == 0.0
    r = one(cand(0.47, 0.06, 0.47, 1.91))
    assert r.qualification_status == NO_POSITIVE_EV and r.full_kelly_fraction == 0.0
    assert r.fractional_kelly_fraction == r.single_bet_capped_fraction == r.final_stake_fraction == 0.0
    assert not r.mathematically_eligible and not r.actionable


@pytest.mark.parametrize("pw,pp,pl,d", [(0.5, 0.0, 0.5, 2.0), (0.4, 0.0, 0.6, 2.4), (0.0, 1.0, 0.0, 1.91),
                                        (0.0, 0.0, 1.0, 5.0), (0.3, 0.4, 0.3, 2.0)])
def test_ev_le_zero_is_zero_kelly(pw, pp, pl, d):
    assert kelly.expected_value(pw, pp, pl, d) <= 1e-15
    assert kelly.full_kelly_fraction(pw, pp, pl, d) == 0.0


def test_push_probability_near_one():
    """push 幾乎必然：輸的機會很小 → full Kelly 可以很大（數學正確），由 ¼ 與 2% 上限控制。"""
    pw, pp, pl, d = 0.015, 0.98, 0.005, 1.91
    f = kelly.full_kelly_fraction(pw, pp, pl, d)
    assert f == pytest.approx((0.015 * 0.91 - 0.005) / (0.91 * 0.02), abs=1e-12) and 0.47 < f < 0.48
    res = minimize_scalar(lambda x: -kelly.expected_log_growth(x, pw, pp, pl, d), bounds=(0, 0.999),
                          method="bounded", options={"xatol": 1e-12})
    assert res.x == pytest.approx(f, abs=1e-6)
    r = one(cand(pw, pp, pl, d))
    assert r.single_bet_capped_fraction == RISK_V1.max_bet_fraction
    # push = 1：沒有輸贏 → EV 0 → Kelly 0（不除以 0）
    assert kelly.full_kelly_fraction(0.0, 1.0, 0.0, 1.91) == 0.0
    # 不會輸（p_loss = 0）→ f* = 1（公式與單調性一致），之後一樣被上限截斷
    assert kelly.full_kelly_fraction(0.3, 0.7, 0.0, 1.91) == 1.0


@pytest.mark.parametrize("d,ok", [(1.0, False), (0.95, False), (float("nan"), False), (float("inf"), False),
                                  (None, False), ("x", False), (1.0000001, True), (1001.0, True)])
def test_decimal_odds_edge_cases(d, ok):
    if ok:
        assert kelly.full_kelly_fraction(0.5, 0.0, 0.5, d) >= 0.0
        assert kelly.full_kelly_fraction(0.01, 0.0, 0.99, 1001.0) == pytest.approx((0.01 * 1000 - 0.99) / 1000)
    else:
        with pytest.raises(kelly.KellyInputError):
            kelly.full_kelly_fraction(0.5, 0.0, 0.5, d)


@pytest.mark.parametrize("ps", [(0.6, 0.0, 0.5), (-0.01, 0.0, 1.01), (0.5, 0.0, float("nan")), (1.2, -0.2, 0.0)])
def test_invalid_probabilities_rejected(ps):
    with pytest.raises(kelly.KellyInputError):
        kelly.full_kelly_fraction(*ps, 1.91)
    r = one(cand(*ps, 1.91, ev=0.01))
    assert r.qualification_status == INVALID_PROBABILITY and r.full_kelly_fraction is None
    assert r.final_stake_fraction == 0.0 and not r.actionable


def test_stored_ev_must_match_probabilities_and_odds():
    r = one(cand(0.55, 0.0, 0.45, 1.91, ev=0.08))
    assert r.qualification_status == INVALID_PROBABILITY and "invalid_probability:ev_mismatch" in r.reasons
    r = one(cand(0.55, 0.01, 0.44, 1.91, rule="half_line_no_push"))
    assert r.qualification_status == INVALID_PROBABILITY and "invalid_probability:push_on_no_push_market" in r.reasons


# ======================= Three-way ======================= #

def h1_three_way(p_home, p_draw, p_away, odds=(1.55, 10.0, 2.45), snap=60, **kw):
    out = []
    for side, pw, d in (("home", p_home, odds[0]), ("draw", p_draw, odds[1]), ("away", p_away, odds[2])):
        out.append(cand(pw, 0.0, 1.0 - pw, d, side=side, snap=snap, market="h1_ml", source="twsport", book="twsport",
                        rule="three_way_draw_outcome", market_type="moneyline", period=H1, outcome_set=THREE_WAY,
                        line=None, **kw))
    return out


def test_draw_kelly_uses_single_outcome_formula():
    """和局：p_win = P(draw)、p_loss = 1 − P(draw)、push = 0 → 一般 Kelly。"""
    rs = size_portfolio(h1_three_way(0.40, 0.12, 0.48, odds=(2.30, 10.0, 1.95)), as_of=T)
    draw = next(r for r in rs if r.side == "draw")
    assert draw.p_push == 0.0 and draw.p_loss == pytest.approx(0.88)
    assert draw.full_kelly_fraction == pytest.approx((0.12 * 9 - 0.88) / 9, abs=1e-15)          # 0.0222…
    assert draw.qualification_status == ELIGIBLE
    assert draw.single_bet_capped_fraction == pytest.approx(0.25 * (0.12 * 9 - 0.88) / 9)
    assert [r.side for r in rs if r.mathematically_eligible] == ["draw"]


def test_three_way_other_outcomes_are_losses():
    """主隊（三向）：P(h1 = 0) 歸入 loss（D.2），不是 push → Kelly 不會把和局當退款。"""
    rs = size_portfolio(h1_three_way(0.60, 0.035, 0.365, odds=(1.80, 10.0, 2.45)), as_of=T)
    home = next(r for r in rs if r.side == "home")
    assert home.p_push == 0.0 and home.p_loss == pytest.approx(0.40)
    assert home.full_kelly_fraction == pytest.approx((0.6 * 0.8 - 0.4) / 0.8, abs=1e-15)
    pushy = kelly.full_kelly_fraction(0.6, 0.035, 0.365, 1.80)                                   # 錯誤：和局當 push
    assert home.full_kelly_fraction != pytest.approx(pushy)
    assert [r.side for r in rs if r.mathematically_eligible] == ["home"]


def test_three_way_only_one_outcome_can_be_sized():
    """2.2 / 5.0 / 2.2（overround 10.9%）+ 0.46 / 0.08 / 0.46 → 主、客都正 EV → 整個市場拒絕（不同時下注互斥 outcome）。"""
    rs = size_portfolio(h1_three_way(0.46, 0.08, 0.46, odds=(2.2, 5.0, 2.2)), as_of=T)
    assert all(r.qualification_status == MUTUALLY_EXCLUSIVE for r in rs)
    assert all(r.final_stake_fraction == 0.0 and not r.actionable and not r.mathematically_eligible for r in rs)
    assert "mutually_exclusive_positive_kelly:away,home" in rs[0].reasons


# ======================= Unsupported / hard rejects ======================= #

def test_h1_two_way_moneyline_unknown_settlement_never_sized():
    c = cand(None, None, None, 1.9, market="h1_ml", status="unsupported_settlement",
             rule="h1_two_way_tie_settlement_unknown", market_type="moneyline", period=H1, ev=None)
    r = one(c)
    assert r.qualification_status == UNSUPPORTED_SETTLEMENT and r.full_kelly_fraction is None
    # defense in depth：即使定價列被誤標為 priced（且有機率），兩向上半場獨贏仍不計算
    c2 = cand(0.6, 0.0, 0.4, 1.9, market="h1_ml", rule="moneyline_ot_included", market_type="moneyline", period=H1)
    r2 = one(c2)
    assert r2.qualification_status == UNSUPPORTED_SETTLEMENT and r2.final_stake_fraction == 0.0
    assert "settlement:h1_two_way_tie_settlement_unknown" in r2.reasons


def test_quarter_line_never_sized():
    c = cand(None, None, None, 1.9, status="unsupported_settlement", rule="quarter_line_split_settlement", ev=None,
             line=-5.25)
    assert one(c).qualification_status == UNSUPPORTED_SETTLEMENT
    c2 = cand(0.6, 0.0, 0.4, 1.9, line=-5.25, display_line=-5.25)                                  # 誤標 priced
    r = one(c2)
    assert r.qualification_status == UNSUPPORTED_SETTLEMENT and "settlement:quarter_line_split_settlement" in r.reasons


def test_missing_model_is_no_prediction():
    c = cand(None, None, None, 1.9, status="market_only", prediction_id=None, ev=None,
             pricing_status_reason="no_valid_prediction_at_as_of")
    r = one(c)
    assert r.qualification_status == NO_PREDICTION and r.full_kelly_fraction is None and not r.actionable


@pytest.mark.parametrize("why", ["market_not_open:suspended", "market_not_open:closed"])
def test_closed_or_suspended_market_never_sized(why):
    r = one(cand(0.6, 0.0, 0.4, 1.95, status="rejected", pricing_status_reason=why))
    assert r.qualification_status == MARKET_NOT_OPEN and r.final_stake_fraction == 0.0


def test_started_game_and_invalid_rows():
    assert one(cand(0.6, 0.0, 0.4, 1.95, start=T)).qualification_status == MARKET_NOT_OPEN          # 已開賽
    r = one(cand(0.6, 0.0, 0.4, 1.95, status="rejected", pricing_status_reason="implausible_overround"))
    assert r.qualification_status == UNAVAILABLE and r.reasons == ["invalid_pricing_row:implausible_overround"]
    r = one(cand(0.6, 0.0, 0.4, 1.95, preset_unavailable="artifact_mismatch"))
    assert r.qualification_status == UNAVAILABLE and r.reasons == ["artifact_mismatch"]
    r = one(cand(0.6, 0.0, 0.4, 1.95, status="unsupported_market", rule="full_game_three_way_regulation_not_modelled"))
    assert r.qualification_status == UNSUPPORTED_SETTLEMENT


# ======================= Policy ======================= #

def test_risk_v1_is_frozen():
    assert (RISK_V1.version, RISK_V1.kelly_multiplier, RISK_V1.max_bet_fraction, RISK_V1.max_game_fraction,
            RISK_V1.max_day_fraction) == ("risk-v1", 0.25, 0.02, 0.03, 0.08)
    assert policy.PRODUCTION_RISK_POLICY is RISK_V1
    with pytest.raises(Exception):
        RISK_V1.kelly_multiplier = 0.5                                                             # frozen dataclass
    with pytest.raises(TypeError):
        RISK_V1.source_poll_interval_min["twsport"] = 5                                            # 唯讀 mapping
    policy.assert_registered(RISK_V1)
    with pytest.raises(ValueError):                                                                # 不能用 risk-v1 名字寫別的參數
        policy.assert_registered(replace(RISK_V1, kelly_multiplier=0.5))
    with pytest.raises(ValueError):
        RiskPolicy("x", 1.5, 0.02, 0.03, 0.08, 2.0)
    # 輪詢間隔 = D.1 宣告的來源間隔
    from core.odds.ingest import SOURCES
    assert {k: v["expected_interval_min"] for k, v in SOURCES.items()} == dict(RISK_V1.source_poll_interval_min)


def test_kelly_fraction_env_setting_removed():
    import core.config as cfg
    assert not hasattr(cfg.settings, "kelly_fraction")
    assert "KELLY_FRACTION" not in (REPO / "pipeline" / "core" / "config.py").read_text()


def test_quarter_kelly_exactly():
    for pw, d in ((0.55, 1.91), (0.53, 1.95), (0.36, 3.1)):
        r = one(cand(pw, 0.0, 1 - pw, d))
        assert r.fractional_kelly_fraction == r.full_kelly_fraction * 0.25


def test_two_percent_per_bet_cap():
    r = one(cand(0.62, 0.0, 0.38, 2.0))                                                           # full 0.24 → ¼ 0.06
    assert r.full_kelly_fraction == pytest.approx(0.24) and r.fractional_kelly_fraction == pytest.approx(0.06)
    assert r.single_bet_capped_fraction == 0.02 == r.final_stake_fraction
    assert r.qualification_status == ELIGIBLE                                                      # 單筆上限不是 exposure scaling


def test_no_heuristic_confidence_multiplication():
    base = one(cand(0.55, 0.0, 0.45, 1.91))
    flagged = one(cand(0.55, 0.0, 0.45, 1.91, data_quality_flags=("season_opener", "low_sample", "injury_unknown",
                                                                   "missing_box_recent", "pending_prior_game"),
                       pricing_warnings=("ml_logistic_margin_divergence",)))
    for k in ("full_kelly_fraction", "fractional_kelly_fraction", "single_bet_capped_fraction", "final_stake_fraction"):
        assert getattr(flagged, k) == getattr(base, k)
    assert flagged.qualification_status == ELIGIBLE and flagged.actionable
    assert "data_quality:season_opener" in flagged.warnings and "ml_logistic_margin_divergence" in flagged.warnings
    src = (REPO / "pipeline" / "core" / "sizing" / "engine.py").read_text()
    assert "confidence" not in src.lower()


def test_edge_is_not_used_as_kelly_input():
    a = one(cand(0.55, 0.0, 0.45, 1.91, edge_vs_fair=0.05, fair_no_vig_prob=0.50))
    b = one(cand(0.55, 0.0, 0.45, 1.91, edge_vs_fair=-0.20, fair_no_vig_prob=0.75))
    assert a.full_kelly_fraction == b.full_kelly_fraction and a.final_stake_fraction == b.final_stake_fraction
    # D.2 台彩高水例：edge +5 pp 但 EV −3.75% → Kelly 0
    r = one(cand(0.55, 0.0, 0.45, 1.75, edge_vs_fair=0.05, source="twsport", book="twsport"))
    assert r.ev_per_unit == pytest.approx(-0.0375) and r.full_kelly_fraction == 0.0
    assert r.qualification_status == NO_POSITIVE_EV
    tree = ast.parse((REPO / "pipeline" / "core" / "sizing" / "kelly.py").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {a.arg for n in ast.walk(tree)
                                                                       if isinstance(n, ast.arguments) for a in n.args}
    assert not {"edge", "edge_vs_fair", "fair", "fair_no_vig_prob"} & names


def test_no_minimum_ev_threshold_small_positive_ev_gets_small_stake():
    r = one(cand(0.5240, 0.0, 0.4760, 1.91))                                                       # EV ≈ +0.08%
    assert 0 < r.ev_per_unit < 0.001 and r.qualification_status == ELIGIBLE
    assert 0 < r.final_stake_fraction < 0.0005


# ======================= Game cap ======================= #

def test_game_cap_below_three_percent_unchanged():
    rs = size_portfolio([cand(0.55, 0.0, 0.45, 1.91, market="spread", snap=1),
                         cand(0.55, 0.0, 0.45, 1.91, market="total", snap=2, side="over")], as_of=T)
    assert all(r.game_scale_factor == 1.0 and r.qualification_status == ELIGIBLE for r in rs)
    assert all(r.final_stake_fraction == r.single_bet_capped_fraction for r in rs)
    assert rs[0].game_exposure_before == rs[0].game_exposure_after == pytest.approx(2 * rs[0].single_bet_capped_fraction)


def test_game_cap_above_three_percent_scaled_proportionally():
    cs = [cand(0.62, 0.0, 0.38, 2.0, market="spread", snap=1),                        # capped 0.02
          cand(0.55, 0.0, 0.45, 1.91, market="total", side="over", snap=2),           # 0.01387
          cand(0.60, 0.0, 0.40, 1.80, market="ml", snap=3)]                           # full 0.1 → 0.025 → 0.02
    rs = size_portfolio(cs, as_of=T)
    before = math.fsum(r.single_bet_capped_fraction for r in rs)
    assert before > 0.03
    for r in rs:
        assert r.game_exposure_before == pytest.approx(before) and r.game_scale_factor == pytest.approx(0.03 / before)
        assert r.final_stake_fraction == pytest.approx(r.single_bet_capped_fraction * 0.03 / before, abs=1e-15)
        assert r.qualification_status == EXPOSURE_SCALED and "game_exposure_scaled" in r.reasons and r.actionable
    assert math.fsum(r.final_stake_fraction for r in rs) == pytest.approx(0.03, abs=1e-15)
    # 比例不變（不挑最高 EV 一筆）
    ratios = [r.final_stake_fraction / r.single_bet_capped_fraction for r in rs]
    assert max(ratios) - min(ratios) < 1e-12 and all(r.final_stake_fraction > 0 for r in rs)


def test_game_cap_counts_ml_spread_total_and_h1_together():
    cs = [cand(0.60, 0.0, 0.40, 1.80, market="ml", snap=1),
          cand(0.58, 0.0, 0.42, 1.91, market="spread", snap=2),
          cand(0.58, 0.0, 0.42, 1.91, market="total", side="over", snap=3),
          cand(0.58, 0.0, 0.42, 1.91, market="h1_spread", snap=4, period=H1),
          cand(0.58, 0.0, 0.42, 1.91, market="h1_total", side="over", snap=5, period=H1)]
    rs = size_portfolio(cs, as_of=T)
    assert len({r.game_exposure_before for r in rs}) == 1 and rs[0].game_scale_factor < 1
    assert math.fsum(r.final_stake_fraction for r in rs) == pytest.approx(0.03, abs=1e-15)


def test_multiple_books_same_game_count_toward_same_game_cap():
    """同一 outcome 在三家 bookmaker 都正 Kelly：各自是一個機會（不挑最佳盤），但合計受同一場上限。"""
    cs = [cand(0.62, 0.0, 0.38, 2.0, book=b, snap=i, source="twsport" if b == "twsport" else "oddsapi")
          for i, b in enumerate(("pinnacle", "draftkings", "twsport"), start=1)]
    rs = size_portfolio(cs, as_of=T)
    assert len(rs) == 3 and all(r.single_bet_capped_fraction == 0.02 for r in rs)
    assert rs[0].game_exposure_before == pytest.approx(0.06) and rs[0].game_scale_factor == pytest.approx(0.5)
    assert all(r.final_stake_fraction == pytest.approx(0.01) for r in rs)


# ======================= Daily cap ======================= #

def _game_with_stakes(gid, n=2, start=TIP):
    return [cand(0.62, 0.0, 0.38, 2.0, game_id=gid, market=m, snap=gid * 10 + i, start=start,
                 side="home" if m != "total" else "over")
            for i, m in enumerate(("spread", "total", "ml")[:n])]


def test_daily_cap_below_eight_percent_unchanged():
    rs = size_portfolio(_game_with_stakes(1, 1) + _game_with_stakes(2, 1) + _game_with_stakes(3, 1), as_of=T)
    assert all(r.daily_scale_factor == 1.0 and r.final_stake_fraction == 0.02 for r in rs)
    assert rs[0].daily_exposure_before == pytest.approx(0.06) == rs[0].daily_exposure_after


def test_daily_cap_above_eight_percent_scaled_using_game_capped_input():
    """4 場 × (0.02 + 0.02) → 每場 game cap 後 0.03 → 當日 0.12 > 0.08 → × 2/3。"""
    cs = [c for g in (1, 2, 3, 4) for c in _game_with_stakes(g, 2)]
    rs = size_portfolio(cs, as_of=T)
    for r in rs:
        assert r.game_exposure_before == pytest.approx(0.04) and r.game_adjusted_fraction == pytest.approx(0.015)
        assert r.daily_exposure_before == pytest.approx(0.12)                     # 用 game-adjusted（0.12），不是 0.16
        assert r.daily_scale_factor == pytest.approx(0.08 / 0.12)
        assert r.final_stake_fraction == pytest.approx(0.01, abs=1e-15)
        assert r.qualification_status == EXPOSURE_SCALED and "daily_exposure_scaled" in r.reasons
    assert math.fsum(r.final_stake_fraction for r in rs) == pytest.approx(0.08, abs=1e-15)
    assert rs[0].daily_exposure_after == pytest.approx(0.08)


def test_daily_cap_is_per_taipei_betting_day():
    day1 = [c for g in (1, 2, 3) for c in _game_with_stakes(g, 2)]
    day2 = [c for g in (4, 5, 6) for c in _game_with_stakes(g, 2, start=TIP + timedelta(days=1))]
    rs = size_portfolio(day1 + day2, as_of=T)
    by_day = {}
    for r in rs:
        by_day.setdefault(r.betting_day, []).append(r)
    assert sorted(by_day) == [date(2026, 10, 22), date(2026, 10, 23)]
    for d, xs in by_day.items():
        assert math.fsum(x.final_stake_fraction for x in xs) == pytest.approx(0.08, abs=1e-15)   # 0.09 → 0.08（各日）


def test_non_actionable_rows_do_not_consume_exposure():
    cs = _game_with_stakes(1, 2) + [cand(0.62, 0.0, 0.38, 2.0, game_id=1, market="h1_spread", snap=99,
                                         source="twsport", book="twsport",
                                         last_seen=T - timedelta(hours=5))]                    # 舊盤 → stale
    rs = size_portfolio(cs, as_of=T)
    stale = next(r for r in rs if r.market == "h1_spread")
    assert stale.qualification_status == STALE_QUOTE and stale.mathematically_eligible and not stale.actionable
    assert stale.final_stake_fraction == 0.0 and stale.single_bet_capped_fraction == 0.02
    assert stale.game_exposure_before == pytest.approx(0.04)                                       # 不含 stale


# ======================= Mutual exclusivity ======================= #

def test_both_sides_positive_kelly_rejects_market():
    """2.2 / 2.2（同一 bookmaker 同一時刻的套利，overround < 0）+ 0.5 / 0.5 → 兩邊都正 EV → 整個市場拒絕、不挑一邊。"""
    rs = size_portfolio(pair(0.5, 0.0, 2.2, 2.2, snap=5), as_of=T)
    assert [r.qualification_status for r in rs] == [MUTUALLY_EXCLUSIVE] * 2
    assert all(r.final_stake_fraction == 0.0 and r.full_kelly_fraction > 0 for r in rs)


def test_two_way_consistent_probabilities_cannot_both_be_positive():
    for ph, pp, oh, oa in ((0.55, 0.0, 1.91, 1.91), (0.47, 0.06, 1.91, 1.91), (0.7, 0.0, 1.4, 2.95),
                           (0.6, 0.0, 1.80, 2.20), (0.48, 0.04, 2.02, 1.95)):
        rs = size_portfolio(pair(ph, pp, oh, oa, snap=1), as_of=T)
        assert sum(r.full_kelly_fraction > 0 for r in rs) <= 1
        assert not any(r.qualification_status == MUTUALLY_EXCLUSIVE for r in rs)


def test_other_markets_unaffected_by_rejected_market():
    rs = size_portfolio(pair(0.5, 0.0, 2.2, 2.2, snap=5) + [cand(0.55, 0.0, 0.45, 1.91, market="total", side="over",
                                                                  snap=6)], as_of=T)
    tot = next(r for r in rs if r.market == "total")
    assert tot.qualification_status == ELIGIBLE and tot.game_exposure_before == pytest.approx(tot.final_stake_fraction)


# ======================= Time / freshness ======================= #

def test_future_pricing_snapshot_is_unavailable():
    r = one(cand(0.55, 0.0, 0.45, 1.91, fetched=T + timedelta(seconds=1)))
    assert r.qualification_status == UNAVAILABLE and r.reasons == ["future_data_violation"]
    c = replace(cand(0.55, 0.0, 0.45, 1.91), pricing_analysis_as_of=T + timedelta(minutes=1))
    assert one(c).qualification_status == UNAVAILABLE
    # select_candidates：analysis_as_of > T 的定價列不存在於 T
    rows = _db_like(T + timedelta(minutes=1))
    sel = select_candidates(*rows, T)
    assert sel.candidates == [] and sel.pricing_missing and sel.incomplete_days == {date(2026, 10, 22): "pricing_missing"}


def test_stale_quote_behavior():
    # twsport：輪詢 30 分 → 最大 60 分
    fresh = one(cand(0.55, 0.0, 0.45, 1.91, source="twsport", book="twsport", fetched=T - timedelta(hours=3),
                     last_seen=T - timedelta(minutes=59)))
    assert fresh.qualification_status == ELIGIBLE and fresh.actionable
    assert fresh.quote_age_seconds == 3 * 3600 and fresh.last_seen_age_seconds == 59 * 60
    assert fresh.max_quote_age_seconds == 3600 and "quote_not_confirmed_by_latest_poll" in fresh.warnings
    stale = one(cand(0.55, 0.0, 0.45, 1.91, source="twsport", book="twsport", fetched=T - timedelta(hours=3),
                     last_seen=T - timedelta(minutes=61)))
    assert stale.qualification_status == STALE_QUOTE and not stale.actionable and stale.final_stake_fraction == 0
    assert stale.full_kelly_fraction == fresh.full_kelly_fraction                                    # 理論值仍顯示
    # oddsapi：輪詢 6 小時 → 最大 12 小時
    assert one(cand(0.55, 0.0, 0.45, 1.91, last_seen=T - timedelta(hours=11))).actionable
    assert one(cand(0.55, 0.0, 0.45, 1.91, last_seen=T - timedelta(hours=13))).qualification_status == STALE_QUOTE
    # 未知來源 → 無法定義 → 不 actionable（不偷偷假設）
    u = one(cand(0.55, 0.0, 0.45, 1.91, source="other", book="x"))
    assert u.qualification_status == STALE_QUOTE and "stale_quote:poll_interval_unknown:other" in u.reasons
    # 負 EV 的舊盤：狀態仍是 no_positive_ev
    assert one(cand(0.47, 0.06, 0.47, 1.91, last_seen=T - timedelta(days=2))).qualification_status == NO_POSITIVE_EV


def test_last_seen_after_as_of_means_quote_was_live_at_as_of():
    """歷史重建：之後的輪詢仍確認同一報價（fetched_at ≤ T ≤ last_seen_at）→ T 時點掛牌中；年齡以 T 截斷（不得為負）。"""
    r = one(cand(0.55, 0.0, 0.45, 1.91, fetched=T - timedelta(hours=1), last_seen=T + timedelta(hours=3)))
    assert r.last_seen_age_seconds == 0.0 and r.quote_age_seconds == 3600 and r.actionable
    # 舊列沒有 last_seen_at → 用 fetched_at（只能確定那一刻看到）
    r = one(cand(0.55, 0.0, 0.45, 1.91, source="twsport", book="twsport", fetched=T - timedelta(minutes=90),
                 last_seen=None))
    assert r.qualification_status == STALE_QUOTE and r.last_seen_age_seconds == 5400


@pytest.mark.parametrize("start,day", [
    (datetime(2026, 10, 21, 15, 59, 59, tzinfo=UTC), date(2026, 10, 21)),     # 台灣 23:59:59
    (datetime(2026, 10, 21, 16, 0, 0, tzinfo=UTC), date(2026, 10, 22)),       # 台灣 00:00
    (datetime(2026, 10, 21, 23, 30, tzinfo=UTC), date(2026, 10, 22)),         # ET 19:30 → 台灣 07:30 隔日
    (datetime(2026, 12, 25, 17, 0, tzinfo=UTC), date(2026, 12, 26)),          # 聖誕節 ET 中午 → 台灣 12/26 01:00
    (datetime(2026, 12, 26, 3, 0, tzinfo=UTC), date(2026, 12, 26)),           # 聖誕節 ET 22:00 → 同一個台灣日
])
def test_asia_taipei_betting_day_boundary(start, day):
    assert engine.betting_day(start) == day


def test_boundary_games_land_in_different_daily_caps():
    late = [c for g in (1, 2, 3) for c in _game_with_stakes(g, 2, start=datetime(2026, 10, 22, 15, 59, tzinfo=UTC))]
    early = [c for g in (4, 5, 6) for c in _game_with_stakes(g, 2, start=datetime(2026, 10, 22, 16, 0, tzinfo=UTC))]
    rs = size_portfolio(late + early, as_of=T)
    assert {r.betting_day for r in rs} == {date(2026, 10, 22), date(2026, 10, 23)}
    assert all(r.daily_exposure_before == pytest.approx(0.09) for r in rs)                          # 不是 0.18


# ======================= Determinism / portfolio key ======================= #

def test_deterministic_and_order_independent():
    cs = [c for g in (1, 2, 3, 4) for c in _game_with_stakes(g, 3)] + pair(0.5, 0.0, 2.2, 2.2, snap=77)
    a = [r.to_dict() for r in size_portfolio(cs, as_of=T)]
    b = [r.to_dict() for r in size_portfolio(list(reversed(cs)), as_of=T)]
    assert a == b
    assert len({r["portfolio_key"] for r in a}) == 1


def test_portfolio_key_changes_only_when_portfolio_changes():
    cs = _game_with_stakes(1, 2)
    k1 = size_portfolio(cs, as_of=T)[0].portfolio_key
    assert size_portfolio(cs, as_of=T + timedelta(minutes=5))[0].portfolio_key == k1         # 時間前進、狀態不變
    k_new_member = size_portfolio(cs + _game_with_stakes(2, 1), as_of=T)[0].portfolio_key
    assert k_new_member != k1
    old = T - timedelta(hours=13)
    stale = [replace(c, odds_last_seen_at=old, odds_fetched_at=old, pricing_analysis_as_of=old) if i == 0 else c
             for i, c in enumerate(cs)]
    assert size_portfolio(stale, as_of=T)[0].portfolio_key != k1                              # 狀態改變
    other = RiskPolicy("risk-test", 0.25, 0.02, 0.03, 0.08, 2.0, {"oddsapi": 360})
    assert size_portfolio(cs, as_of=T, policy=other)[0].portfolio_key != k1                    # policy 改版


def test_portfolio_incomplete_blocks_actionable():
    rs = size_portfolio(_game_with_stakes(1, 1), as_of=T, incomplete_days={date(2026, 10, 22): "pricing_missing"})
    assert rs[0].qualification_status == UNAVAILABLE and not rs[0].actionable and rs[0].final_stake_fraction == 0
    assert rs[0].single_bet_capped_fraction == 0.02 and "portfolio_incomplete:pricing_missing" in rs[0].warnings


def test_bankroll_amount_is_derived_not_stored():
    r = one(cand(0.55, 0.0, 0.45, 1.91))
    assert r.stake_amount(10000) == pytest.approx(10000 * r.final_stake_fraction)
    assert "stake_amount" not in r.to_dict() and "bankroll" not in " ".join(r.to_dict())
    from core.sizing.job import COLUMNS
    assert not any("bankroll" in c or "amount" in c for c in COLUMNS)
    with pytest.raises(ValueError):
        r.stake_amount(-1)


# ======================= Selection layer / in-memory reconstruction ======================= #

ART = "ml-v2.0+test"


def _pred_row(pid, created, av=ART):
    return {"id": pid, "game_id": 7, "model_version": "ml-v2.0", "created_at": created, "home_win_prob": 0.6,
            "pred_margin": 3.0, "pred_total": 224.0, "pred_home_h1": 56.0, "pred_away_h1": 54.0,
            "features_json": {"artifact_version": av, "profile": "early", "prediction_kind": "early",
                              "prediction_as_of_utc": created.isoformat()}}


def _odds_row(i, at, status="open", **kw):
    return {"id": i, "game_id": 7, "source": "twsport", "bookmaker": "twsport", "market": "ml",
            "market_type": "moneyline", "period": "full_game", "outcome_set": "two_way", "model_threshold": 0.0,
            "market_status": status, "fetched_at": at, "last_seen_at": at, "home_odds": 1.8, "away_odds": 2.0, **kw}


def _pricing_rows(odds_id, pid, as_of, av=ART, p_home=0.60):
    out = []
    for i, (side, pw, pl, d) in enumerate((("home", p_home, 1 - p_home, 1.8), ("away", 1 - p_home, p_home, 2.0))):
        out.append({"id": odds_id * 100 + (pid or 0) * 10 + i, "pricing_version": "pricing-v1",
                    "odds_snapshot_id": odds_id, "prediction_id": pid, "game_id": 7, "analysis_as_of": as_of,
                    "odds_fetched_at": as_of, "status": "priced" if pid else "market_only",
                    "status_reason": None if pid else "no_valid_prediction_at_as_of",
                    "settlement_rule": "moneyline_ot_included", "source": "twsport", "bookmaker": "twsport",
                    "market": "ml", "market_type": "moneyline", "period": "full_game", "outcome_set": "two_way",
                    "line": None, "side": side, "display_line": None, "decimal_odds": d,
                    "model_prob": pw if pid else None, "push_prob": 0.0 if pid else None,
                    "loss_prob": pl if pid else None,
                    "ev_per_unit": pricing_engine.expected_value(pw, 0.0, pl, d) if pid else None,
                    "edge_vs_fair": 0.0, "artifact_version": av if pid else None, "warnings": "[]",
                    "diagnostics": "{}"})
    return out


def _db_like(pricing_as_of):
    games = [{"id": 7, "date_utc": TIP}]
    odds = [_odds_row(1, T - timedelta(minutes=30))]
    preds = [_pred_row(3, T - timedelta(hours=2))]
    pricing = _pricing_rows(1, 3, pricing_as_of)
    return games, odds, preds, pricing


def test_select_candidates_uses_latest_snapshot_and_latest_prediction():
    games, odds, preds, pricing = _db_like(T - timedelta(minutes=30))
    odds.append(_odds_row(2, T + timedelta(minutes=5), home_odds=1.5))                   # 之後的線動：看不到
    preds.append(_pred_row(4, T + timedelta(minutes=1)))                                 # 之後的預測：看不到
    pricing += _pricing_rows(1, None, T - timedelta(hours=3))                            # 舊的 market_only：不是目前預測
    sel = select_candidates(games, odds, preds, pricing, T)
    assert [(c.odds_snapshot_id, c.prediction_id, c.side) for c in sel.candidates] == [(1, 3, "home"), (1, 3, "away")]
    assert not sel.incomplete_days
    rs = size_portfolio(sel.candidates, as_of=T, incomplete_days=sel.incomplete_days)
    home = next(r for r in rs if r.side == "home")
    assert home.full_kelly_fraction == pytest.approx((0.6 * 0.8 - 0.4) / 0.8) and home.actionable


def test_select_candidates_pricing_lag_and_artifact_mismatch_and_closed():
    games, odds, preds, pricing = _db_like(T - timedelta(minutes=30))
    preds.append(_pred_row(5, T - timedelta(minutes=10)))                                 # 新預測、尚未定價
    sel = select_candidates(games, odds, preds, pricing, T)
    assert sel.candidates == [] and sel.pricing_missing == [{"game_id": 7, "odds_snapshot_id": 1, "prediction_id": 5}]
    games, odds, preds, pricing = _db_like(T - timedelta(minutes=30))
    pricing = _pricing_rows(1, 3, T - timedelta(minutes=30), av="ml-v2.0+other")
    sel = select_candidates(games, odds, preds, pricing, T)
    rs = size_portfolio(sel.candidates, as_of=T)
    assert {r.qualification_status for r in rs} == {UNAVAILABLE} and rs[0].reasons == ["artifact_mismatch"]
    games, odds, preds, pricing = _db_like(T - timedelta(minutes=30))
    odds.append(_odds_row(2, T - timedelta(minutes=5), status="suspended"))
    sel = select_candidates(games, odds, preds, pricing, T)
    assert sel.candidates == [] and sel.skipped_not_open == 1                            # 最新是 suspended → 不可下注


def test_in_memory_reconstruction_path_matches():
    """D.4：price_game_as_of 的記憶體輸出直接 sizing，與 DB 列路徑同樣的結果（定價 → sizing 不需要先寫入）。"""
    class Stub:
        def line(self, target, threshold):
            return {"probability_above": 0.6, "probability_push": 0.0, "probability_below": 0.4,
                    "artifact_version": ART, "distribution_version": "dist-v2", "data_quality": {"flags": []}}

    snap = make_snapshot(source="twsport", bookmaker="twsport", source_event_id="e", market_type=MONEYLINE,
                         period=FULL_GAME, status="open", prices={"home": 1.8, "away": 2.0})
    p = PredictionInput(3, 7, "ml-v2.0", T - timedelta(hours=2), T - timedelta(hours=2), ART, "early", "early", 0.6)
    mp = price_market(OddsInput(1, 7, T - timedelta(minutes=30), snap, last_seen_at=T - timedelta(minutes=5)), p,
                      Stub(), as_of=T)
    rs = size_pricings([mp], {7: TIP}, T)
    home = next(r for r in rs if r.side == "home")
    assert home.market_pricing_snapshot_id is None and home.odds_snapshot_id == 1
    assert home.full_kelly_fraction == pytest.approx((0.6 * 0.8 - 0.4) / 0.8) and home.actionable
    assert home.last_seen_age_seconds == 300


# ======================= Serialization / no TS Kelly ======================= #

def test_result_serialization_is_json_safe_and_covers_columns():
    import json

    from core.sizing.job import COLUMNS
    rs = size_portfolio(_game_with_stakes(1, 2) + pair(0.5, 0.0, 2.2, 2.2, snap=9), as_of=T)
    for r in rs:
        d = r.to_dict()
        json.dumps(d, allow_nan=False)
        assert set(COLUMNS) <= set(d)
        assert d["qualification_status"] in engine.QUALIFICATION_STATUSES


_SIZING_WORDS = re.compile(r"kelly|stake_fraction|stake_amount|capped|scale_factor|exposure|bankroll|max_(bet|game|day)", re.I)
_ARITH = re.compile(r"[A-Za-z0-9_)\]]\s*[*/]\s*[A-Za-z0-9_(.]|Math\.(min|max)\s*\(")


def _code_fragments(text: str) -> list[tuple[int, str]]:
    """JS / TS 的「程式碼」片段：去掉註解與字串內容，但保留樣板字串裡的 ${…} 運算式（HTML 文字不檢查）。"""
    out = []
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
    for n, line in enumerate(text.splitlines(), 1):
        line = re.sub(r"(^|\s)//.*$", "", line)
        exprs = re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", line)
        code = re.sub(r"`[^`]*`|'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"", "''", line)
        if "<" in code and ">" in code:                     # 多行樣板內的 HTML 列：只看 ${…}
            code = ""
        out.extend((n, frag) for frag in [code, *exprs] if frag.strip())
    return out


def test_no_kelly_or_stake_math_in_typescript_or_frontend():
    """Kelly / 上限 / exposure 縮放只在 Python：TS 與前端程式碼中，提到這些量的運算式不得有 * / Math.min / Math.max。
    （個人投注紀錄的 payout = 使用者自填 stake × 賠率 是結算，不是 sizing，不在檢查範圍。）"""
    files = sorted(set((REPO / "src").rglob("*.ts")) | set((REPO / "src").rglob("*.tsx")) |
                   set((REPO / "public" / "static" / "js").glob("*.js")))
    bad = [f"{f.relative_to(REPO)}:{n}: {frag.strip()}" for f in files for n, frag in _code_fragments(f.read_text())
           if _SIZING_WORDS.search(frag) and _ARITH.search(frag)]
    assert bad == [], "\n".join(bad)
    # 序列化層本身完全沒有乘除（只命名 / 分組 / 對照）
    code = " ".join(frag for _, frag in _code_fragments((REPO / "src" / "lib" / "sizing.ts").read_text()))
    assert not re.search(r"[A-Za-z0-9_)\]]\s*[*/]\s*[A-Za-z0-9_(]", code)
    # 掃描器本身會抓到典型的 TS Kelly 寫法
    sample = "const k = (p * b - q) / b * 0.25\nconst stake = Math.min(kelly, 0.02)\nhtml = `<td>${o.kelly * 4}</td>`"
    assert len([1 for _, f in _code_fragments(sample) if _SIZING_WORDS.search(f) and _ARITH.search(f)]) == 2
