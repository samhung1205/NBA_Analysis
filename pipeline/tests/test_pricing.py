"""Phase D.2：去水 / 模型機率對應 / push / 三向 / EV / bookmaker 分離 / 時間對齊（純邏輯，不連 DB）

算術測試用 Stub 取代 C.5E 機率來源，只為了能用手算的精確數字驗證公式；
production 路徑（RowModelProbabilities → probability.py）在 test_pricing_artifact.py 以真實 artifact 驗證。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from core.odds.canonical import (CLOSED, FULL_GAME, H1, MONEYLINE, OPEN, SPREAD, SUSPENDED, THREE_WAY, TOTAL,
                                 MarketSnapshot, make_snapshot)
from core.pricing import engine, novig
from core.pricing.alignment import (latest_snapshots_as_of, odds_input_from_row, price_game_as_of,
                                    select_prediction)
from core.pricing.engine import (MARKET_ONLY, PRICED, REJECTED, UNSUPPORTED_MARKET, UNSUPPORTED_SETTLEMENT,
                                 FutureDataError, ModelProbabilityError, OddsInput, PredictionInput, price_market)
from core.pricing.job import COLUMNS

UTC = timezone.utc
TIP = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)
T0 = TIP - timedelta(hours=6)
ART = "ml-v2.0+test"


class Stub:
    """C.5E 介面形狀的固定機率表：{(target, threshold): (above, push, below)}。記錄被查詢的 (target, threshold)。"""

    def __init__(self, table, artifact_version=ART):
        self.table = {(t, float(x)): v for (t, x), v in table.items()}
        self.calls = []
        self.av = artifact_version

    def line(self, target, threshold):
        self.calls.append((target, float(threshold)))
        a, p, b = self.table[(target, float(threshold))]
        return {"target": target, "line": float(threshold), "probability_above": a, "probability_push": p,
                "probability_below": b, "artifact_version": self.av, "distribution_version": "dist-v2",
                "point_prediction": 0.0, "distribution": "gaussian", "center": 0.0, "scale": 13.8,
                "data_quality": {"flags": []}}


def pred(pid=1, created=T0 - timedelta(hours=1), as_of=None, kind="early", hw=0.6, game_id=7, av=ART):
    return PredictionInput(pid, game_id, "ml-v2.0", created, as_of or created, av, "early", kind, hw)


def odds(snap, sid=10, at=T0, game_id=7):
    return OddsInput(sid, game_id, at, snap)


def snap(mt, period=FULL_GAME, status=OPEN, outcome_set="two_way", source="twsport", bookmaker="twsport", **kw):
    return make_snapshot(source=source, bookmaker=bookmaker, source_event_id="e1", market_type=mt, period=period,
                         status=status, outcome_set=outcome_set, **kw)


def ml(home, away, **kw):
    return snap(MONEYLINE, prices={"home": home, "away": away}, **kw)


def by_side(mp):
    return {o.side: o for o in mp.outcomes}


# ======================= No-vig ======================= #

def test_known_two_way_example_from_spec():
    """1.91 / 1.91：raw 0.52356 / 0.52356、overround ≈ 4.71%、fair 0.5 / 0.5；model 0.55 → edge +0.05、EV +0.0505。"""
    mp = price_market(odds(ml(1.91, 1.91)), pred(), Stub({("margin", 0): (0.55, 0.0, 0.45)}), as_of=T0)
    h, a = by_side(mp)["home"], by_side(mp)["away"]
    assert mp.status == PRICED and mp.no_vig_method == "proportional-v1"
    assert h.raw_implied_prob == a.raw_implied_prob == 1 / 1.91
    assert round(h.raw_implied_prob, 5) == 0.52356
    assert mp.market_overround == pytest.approx(2 / 1.91 - 1, abs=1e-15) and round(mp.market_overround, 4) == 0.0471
    assert h.fair_no_vig_prob == pytest.approx(0.5, abs=1e-15) and a.fair_no_vig_prob == pytest.approx(0.5, abs=1e-15)
    assert h.edge_vs_fair == pytest.approx(0.05, abs=1e-12)
    assert h.ev_per_unit == pytest.approx(0.55 * 1.91 - 1, abs=1e-12) and h.ev_per_unit == pytest.approx(0.0505)
    assert h.expected_return == pytest.approx(1.0505) and h.ev_percent == pytest.approx(5.05)
    assert a.edge_vs_fair == pytest.approx(-0.05, abs=1e-12) and a.ev_per_unit == pytest.approx(0.45 * 1.91 - 1)


def test_overround_and_fair_known_asymmetric_example():
    r = novig.proportional_no_vig([1.80, 2.10])
    q1, q2 = 1 / 1.8, 1 / 2.1
    assert r.raw_implied == (q1, q2) and r.total_raw_implied == pytest.approx(q1 + q2)
    assert r.overround == pytest.approx(q1 + q2 - 1) and round(r.overround, 6) == 0.031746
    assert r.fair == pytest.approx((q1 / (q1 + q2), q2 / (q1 + q2))) and round(r.fair[0], 6) == 0.538462
    assert sum(r.fair) == pytest.approx(1.0, abs=1e-12) and r.fair_prob_sum == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize("prices", [(1.91, 1.91), (1.42, 2.95), (1.75, 1.68), (1.01, 15.0), (3.3, 1.33)])
def test_two_way_fair_probabilities_sum_to_one(prices):
    r = novig.proportional_no_vig(list(prices))
    assert math.fsum(r.fair) == pytest.approx(1.0, abs=1e-12)
    assert all(0 < f < 1 for f in r.fair)


def test_three_way_fair_sums_to_one_and_is_not_split_into_two_way():
    r = novig.proportional_no_vig([1.70, 10.0, 1.70])
    s = 1 / 1.7 + 0.1 + 1 / 1.7
    assert r.overround == pytest.approx(s - 1) and round(r.overround, 4) == 0.2765       # 台彩上半場實測
    assert r.fair == pytest.approx((1 / 1.7 / s, 0.1 / s, 1 / 1.7 / s))
    assert math.fsum(r.fair) == pytest.approx(1.0, abs=1e-12)
    two_way_home = (1 / 1.7) / (2 / 1.7)                                                 # 錯誤做法：主客兩向
    assert r.fair[0] != pytest.approx(two_way_home)


def test_incomplete_market_is_rejected_not_priced():
    s = MarketSnapshot("twsport", "twsport", "e1", MONEYLINE, FULL_GAME, "two_way", OPEN, None, None,
                       home_odds=1.9, away_odds=None)
    mp = price_market(odds(s), pred(), Stub({}), as_of=T0)
    assert mp.status == REJECTED and mp.status_reason == "incomplete_market"
    assert mp.market_overround is None and all(o.fair_no_vig_prob is None and o.ev_per_unit is None
                                               for o in mp.outcomes)
    s3 = MarketSnapshot("twsport", "twsport", "e1", MONEYLINE, H1, THREE_WAY, OPEN, None, None,
                        home_odds=1.7, away_odds=1.7, draw_odds=None)
    assert price_market(odds(s3), pred(), Stub({}), as_of=T0).status_reason == "incomplete_market"


@pytest.mark.parametrize("status", [SUSPENDED, CLOSED, "unknown"])
def test_suspended_or_closed_market_is_rejected(status):
    s = ml(1.9, 1.9, status=status)
    mp = price_market(odds(s), pred(), Stub({("margin", 0): (0.5, 0, 0.5)}), as_of=T0)
    assert mp.status == REJECTED and mp.status_reason == f"market_not_open:{status}"
    assert all(o.fair_no_vig_prob is None and o.model_prob is None for o in mp.outcomes)


def test_vig_sanity_checks():
    with pytest.raises(novig.NoVigError) as e:
        novig.proportional_no_vig([2.2, 2.2])                                  # Σq < 1：同一 bookmaker 不可能
    assert e.value.reason == "negative_overround"
    with pytest.raises(novig.NoVigError) as e:
        novig.proportional_no_vig([1.1, 1.1])                                  # 81.8%：解析錯誤
    assert e.value.reason == "implausible_overround"
    with pytest.raises(novig.NoVigError):
        novig.proportional_no_vig([float("nan"), 1.9])
    with pytest.raises(novig.NoVigError):
        novig.proportional_no_vig([1.0, 1.9])
    assert novig.proportional_no_vig([1.5, 1.5]).warnings == ("overround_high",)          # 33%：照算 + 警示
    assert novig.proportional_no_vig([2.0, 1.995]).warnings == ("overround_very_low",)
    for p in ([1.94, 1.52], [1.75, 1.68], [1.42, 2.95], [1.91, 1.91]):                    # 台彩 / 美國實測：不警示
        assert novig.proportional_no_vig(p).warnings == ()
    assert novig.proportional_no_vig([1.7, 10.0, 1.7]).warnings == ()                     # 台彩三向 27.7%
    s = MarketSnapshot("x", "x", "e", MONEYLINE, FULL_GAME, "two_way", OPEN, None, None, home_odds=2.2, away_odds=2.2)
    mp = price_market(odds(s), pred(), Stub({}), as_of=T0)
    assert mp.status == REJECTED and mp.status_reason == "negative_overround"


def test_never_combines_sides_from_different_snapshots():
    """較晚的列缺一邊（非 open）→ 該時點不可定價；不會拿較早列的另一邊拼起來。"""
    rows = [
        {"id": 1, "game_id": 7, "source": "twsport", "bookmaker": "twsport", "market": "ml", "market_type": "moneyline",
         "period": "full_game", "outcome_set": "two_way", "market_status": "open", "fetched_at": T0 - timedelta(hours=1),
         "home_odds": 1.9, "away_odds": 1.9, "model_threshold": 0.0},
        {"id": 2, "game_id": 7, "source": "twsport", "bookmaker": "twsport", "market": "ml", "market_type": "moneyline",
         "period": "full_game", "outcome_set": "two_way", "market_status": "suspended", "fetched_at": T0,
         "home_odds": 1.8, "away_odds": None, "model_threshold": 0.0},
    ]
    latest = latest_snapshots_as_of(rows, T0)
    assert [r["id"] for r in latest] == [2]
    mp = price_market(odds_input_from_row(latest[0]), pred(), Stub({}), as_of=T0)
    assert mp.status == REJECTED and by_side(mp)["away"].decimal_odds is None


# ======================= Model mapping ======================= #

def test_full_game_moneyline_mapping():
    st = Stub({("margin", 0): (0.62, 0.0, 0.38)})
    mp = price_market(odds(ml(1.6, 2.4)), pred(), st, as_of=T0)
    h, a = by_side(mp)["home"], by_side(mp)["away"]
    assert st.calls == [("margin", 0.0)]
    assert (h.model_prob, h.push_prob, h.loss_prob) == (0.62, 0.0, 0.38)               # P(margin > 0)
    assert (a.model_prob, a.push_prob, a.loss_prob) == (0.38, 0.0, 0.62)               # P(margin < 0)
    assert mp.settlement_rule == "moneyline_ot_included"


def test_home_favourite_spread_mapping():
    """主讓 5.5（顯示線 −5.5）→ threshold +5.5：主 = P(margin > 5.5)、客 = P(margin < 5.5)。"""
    st = Stub({("margin", 5.5): (0.48, 0.0, 0.52)})
    s = snap(SPREAD, home_line=-5.5, prices={"home": 1.91, "away": 1.91})
    mp = price_market(odds(s), pred(), st, as_of=T0)
    h, a = by_side(mp)["home"], by_side(mp)["away"]
    assert st.calls == [("margin", 5.5)]
    assert (h.model_threshold, h.comparator, h.display_line) == (5.5, "gt", -5.5)
    assert (a.model_threshold, a.comparator, a.display_line) == (5.5, "lt", 5.5)
    assert h.model_prob == 0.48 and a.model_prob == 0.52 and h.push_prob == a.push_prob == 0.0


def test_away_favourite_spread_mapping():
    """客讓 3.5（主隊顯示線 +3.5、客隊 −3.5）→ threshold −3.5：主 = P(margin > −3.5)、客 = P(margin < −3.5)。"""
    st = Stub({("margin", -3.5): (0.57, 0.0, 0.43)})
    s = snap(SPREAD, away_line=-3.5, prices={"home": 1.87, "away": 1.95})
    mp = price_market(odds(s), pred(), st, as_of=T0)
    h, a = by_side(mp)["home"], by_side(mp)["away"]
    assert st.calls == [("margin", -3.5)] and mp.line == 3.5 and mp.away_line == -3.5
    assert h.model_prob == 0.57 and a.model_prob == 0.43


def test_total_over_under_mapping():
    st = Stub({("total", 225.5): (0.46, 0.0, 0.54)})
    s = snap(TOTAL, total_line=225.5, prices={"over": 1.91, "under": 1.91})
    mp = price_market(odds(s), pred(), st, as_of=T0)
    assert st.calls == [("total", 225.5)]
    assert by_side(mp)["over"].model_prob == 0.46 and by_side(mp)["under"].model_prob == 0.54


def test_h1_spread_and_total_use_h1_targets():
    st = Stub({("h1_margin", 2.5): (0.44, 0.0, 0.56), ("h1_total", 112.5): (0.51, 0.0, 0.49)})
    sp = price_market(odds(snap(SPREAD, H1, home_line=-2.5, prices={"home": 1.9, "away": 1.9})), pred(), st, as_of=T0)
    tt = price_market(odds(snap(TOTAL, H1, total_line=112.5, prices={"over": 1.9, "under": 1.9})), pred(), st,
                      as_of=T0)
    assert st.calls == [("h1_margin", 2.5), ("h1_total", 112.5)]
    assert by_side(sp)["home"].model_prob == 0.44 and by_side(tt)["under"].model_prob == 0.49


def test_h1_three_way_moneyline_mapping_and_no_vig():
    st = Stub({("h1_margin", 0): (0.60, 0.035, 0.365)})
    s = snap(MONEYLINE, H1, outcome_set=THREE_WAY, prices={"home": 1.7, "draw": 10.0, "away": 1.7})
    mp = price_market(odds(s), pred(), st, as_of=T0)
    h, d, a = by_side(mp)["home"], by_side(mp)["draw"], by_side(mp)["away"]
    assert mp.status == PRICED and mp.settlement_rule == "three_way_draw_outcome"
    assert (h.model_prob, d.model_prob, a.model_prob) == (0.60, 0.035, 0.365)          # P(>0) / P(=0) / P(<0)
    s3 = 2 / 1.7 + 0.1
    assert d.fair_no_vig_prob == pytest.approx(0.1 / s3) and mp.fair_prob_sum == pytest.approx(1.0)
    assert d.comparator == "eq" and d.model_threshold == 0.0


def test_draw_is_an_outcome_not_a_push():
    st = Stub({("h1_margin", 0): (0.60, 0.035, 0.365)})
    s = snap(MONEYLINE, H1, outcome_set=THREE_WAY, prices={"home": 1.7, "draw": 10.0, "away": 1.7})
    mp = price_market(odds(s), pred(), st, as_of=T0)
    h, d = by_side(mp)["home"], by_side(mp)["draw"]
    assert d.push_prob == 0.0 and d.loss_prob == pytest.approx(0.965)
    assert d.ev_per_unit == pytest.approx(0.035 * 10.0 - 1)                             # 和局 = 中獎
    assert h.push_prob == 0.0 and h.loss_prob == pytest.approx(0.40)                    # 主隊：和局 = 輸（不退款）
    assert h.ev_per_unit == pytest.approx(0.60 * 1.7 - 1)


def test_threshold_sign_convention_partitions_outcomes():
    """同一條線：主 / 客（大 / 小）的 P(win) + P(push) + P(loss) = 1，且主的 win = 客的 loss。"""
    st = Stub({("margin", -7.0): (0.53, 0.03, 0.44)})
    s = snap(SPREAD, home_line=7.0, prices={"home": 1.9, "away": 1.9})
    mp = price_market(odds(s), pred(), st, as_of=T0)
    h, a = by_side(mp)["home"], by_side(mp)["away"]
    assert h.model_threshold == a.model_threshold == -7.0
    assert h.model_prob == a.loss_prob and a.model_prob == h.loss_prob and h.push_prob == a.push_prob == 0.03
    for o in (h, a):
        assert o.model_prob + o.push_prob + o.loss_prob == pytest.approx(1.0, abs=1e-12)


# ======================= Push ======================= #

def test_integer_spread_push_ev_formula_exact():
    """主讓 5 分（threshold +5）：P(win) 0.47 / P(push) 0.06 / P(loss) 0.47，賠率 1.91。"""
    st = Stub({("margin", 5.0): (0.47, 0.06, 0.47)})
    s = snap(SPREAD, home_line=-5.0, prices={"home": 1.91, "away": 1.91})
    mp = price_market(odds(s), pred(), st, as_of=T0)
    h = by_side(mp)["home"]
    assert mp.settlement_rule == "integer_line_push_refund" and h.push_prob == 0.06
    assert h.ev_per_unit == 0.47 * (1.91 - 1) - 0.47
    assert h.ev_per_unit == pytest.approx(0.47 * 1.91 + 0.06 - 1, abs=1e-15)          # 等價形式
    assert h.ev_per_unit == pytest.approx(-0.0423, abs=1e-12)
    assert h.ev_per_unit != pytest.approx(0.47 * 1.91 - 1)                             # push 當輸（錯）= −0.1023
    assert h.ev_per_unit != pytest.approx(0.47 / 0.94 * 1.91 - 1)                      # 條件化後套原賠率（錯）= −0.045


def test_integer_total_push_ev():
    st = Stub({("total", 224.0): (0.49, 0.021, 0.489)})
    s = snap(TOTAL, total_line=224.0, prices={"over": 1.95, "under": 1.87})
    mp = price_market(odds(s), pred(), st, as_of=T0)
    o, u = by_side(mp)["over"], by_side(mp)["under"]
    assert o.ev_per_unit == pytest.approx(0.49 * 0.95 - 0.489, abs=1e-15)
    assert u.ev_per_unit == pytest.approx(0.489 * 0.87 - 0.49, abs=1e-15)
    assert o.push_prob == u.push_prob == 0.021


def test_half_point_line_has_no_push():
    st = Stub({("total", 224.5): (0.5, 0.0, 0.5)})
    mp = price_market(odds(snap(TOTAL, total_line=224.5, prices={"over": 1.9, "under": 1.9})), pred(), st, as_of=T0)
    assert mp.settlement_rule == "half_line_no_push" and all(o.push_prob == 0.0 for o in mp.outcomes)
    bad = Stub({("total", 224.5): (0.5, 0.01, 0.49)})
    with pytest.raises(ModelProbabilityError):
        price_market(odds(snap(TOTAL, total_line=224.5, prices={"over": 1.9, "under": 1.9})), pred(), bad, as_of=T0)


def test_full_game_moneyline_has_no_push():
    """NBA 有延長賽：全場分差不會是 0；模型若給出 push > 0 代表介面錯誤，拒絕定價。"""
    mp = price_market(odds(ml(1.9, 1.9)), pred(), Stub({("margin", 0): (0.5, 0.0, 0.5)}), as_of=T0)
    assert all(o.push_prob == 0.0 for o in mp.outcomes)
    with pytest.raises(ModelProbabilityError):
        price_market(odds(ml(1.9, 1.9)), pred(), Stub({("margin", 0): (0.49, 0.02, 0.49)}), as_of=T0)


def test_expected_value_function_requires_complete_probabilities():
    assert engine.expected_value(0.5, 0.1, 0.4, 2.0) == pytest.approx(0.5 * 1.0 - 0.4)
    with pytest.raises(ValueError):
        engine.expected_value(0.5, 0.1, 0.5, 2.0)


# ======================= Unsupported settlement / market ======================= #

def test_two_way_h1_moneyline_without_settlement_rule_has_no_ev():
    s = snap(MONEYLINE, H1, source="oddsapi", bookmaker="draftkings", prices={"home": 1.55, "away": 2.45})
    st = Stub({("h1_margin", 0): (0.6, 0.035, 0.365)})
    mp = price_market(odds(s), pred(), st, as_of=T0)
    assert mp.status == UNSUPPORTED_SETTLEMENT and mp.status_reason == "h1_two_way_tie_settlement_unknown"
    assert st.calls == []                                                               # 不猜：連模型都不查
    assert mp.market_overround == pytest.approx(1 / 1.55 + 1 / 2.45 - 1)                 # raw / 去水仍保存
    for o in mp.outcomes:
        assert o.fair_no_vig_prob is not None
        assert o.model_prob is None and o.edge_vs_fair is None and o.ev_per_unit is None


def test_quarter_line_and_regulation_three_way_are_unsupported():
    q = price_market(odds(snap(SPREAD, home_line=-2.25, prices={"home": 1.9, "away": 1.9})), pred(), Stub({}),
                     as_of=T0)
    assert q.status == UNSUPPORTED_SETTLEMENT and q.settlement_rule == "quarter_line_split_settlement"
    r = price_market(odds(snap(MONEYLINE, outcome_set=THREE_WAY, prices={"home": 2.0, "draw": 15.0, "away": 2.2})),
                     pred(), Stub({}), as_of=T0)
    assert r.status == UNSUPPORTED_MARKET and all(o.ev_per_unit is None for o in r.outcomes)


def test_no_prediction_is_market_only():
    mp = price_market(odds(ml(1.8, 2.0)), None, None, as_of=T0)
    assert mp.status == MARKET_ONLY and mp.prediction_id is None and mp.analysis_as_of == T0
    assert mp.fair_prob_sum == pytest.approx(1.0) and all(o.model_prob is None for o in mp.outcomes)


# ======================= EV ======================= #

def test_negative_ev_example():
    mp = price_market(odds(ml(1.6, 2.4)), pred(), Stub({("margin", 0): (0.58, 0.0, 0.42)}), as_of=T0)
    h = by_side(mp)["home"]
    assert h.ev_per_unit == pytest.approx(0.58 * 1.6 - 1) and h.ev_per_unit < 0 and h.ev_percent == pytest.approx(-7.2)


def test_ev_uses_offered_odds_not_fair_odds():
    mp = price_market(odds(ml(1.91, 1.91)), pred(), Stub({("margin", 0): (0.55, 0.0, 0.45)}), as_of=T0)
    h = by_side(mp)["home"]
    fair_odds = 1 / h.fair_no_vig_prob                                                  # 2.0
    assert h.ev_per_unit == pytest.approx(0.55 * 1.91 - 1)
    assert h.ev_per_unit != pytest.approx(0.55 * fair_odds - 1)                          # 0.10（錯）


def test_edge_and_ev_are_not_interchangeable():
    """台彩高水：公允 0.5、模型 0.55 → edge +5 pp，但實際賠率 1.75 下 EV 為負。"""
    mp = price_market(odds(ml(1.75, 1.75)), pred(), Stub({("margin", 0): (0.55, 0.0, 0.45)}), as_of=T0)
    h = by_side(mp)["home"]
    assert h.edge_vs_fair == pytest.approx(0.05, abs=1e-12) and h.ev_per_unit == pytest.approx(-0.0375, abs=1e-12)
    assert h.edge_vs_fair > 0 > h.ev_per_unit


# ======================= Bookmakers / lines ======================= #

def _row(i, book, market, at, **kw):
    base = {"id": i, "game_id": 7, "source": "oddsapi" if book != "twsport" else "twsport", "bookmaker": book,
            "market": market, "market_status": "open", "fetched_at": at, "outcome_set": "two_way"}
    if market == "spread":
        base.update(market_type="spread", period="full_game")
    elif market == "ml":
        base.update(market_type="moneyline", period="full_game", model_threshold=0.0)
    return {**base, **kw}


def test_multiple_bookmakers_and_lines_are_priced_separately():
    rows = [_row(1, "pinnacle", "spread", T0, line=-5.5, away_line=5.5, home_odds=1.95, away_odds=1.93),
            _row(2, "draftkings", "spread", T0, line=-5.5, away_line=5.5, home_odds=1.91, away_odds=1.91),
            _row(3, "fanduel", "spread", T0, line=-6.0, away_line=6.0, home_odds=1.95, away_odds=1.87),
            _row(4, "twsport", "spread", T0, line=-5.5, away_line=5.5, home_odds=1.75, away_odds=1.75)]
    st = Stub({("margin", 5.5): (0.52, 0.0, 0.48), ("margin", 6.0): (0.49, 0.03, 0.48)})
    pr = [{"id": 1, "game_id": 7, "model_version": "ml-v2.0", "created_at": T0 - timedelta(hours=2),
           "home_win_prob": 0.7, "pred_margin": 6.0, "pred_total": 225.0, "pred_home_h1": 57.0, "pred_away_h1": 54.0,
           "features_json": {"artifact_version": ART, "profile": "early", "prediction_kind": "early"}}]
    out = price_game_as_of(rows, pr, 7, T0, lambda p: st)
    assert [(m.bookmaker, m.line) for m in out] == [("draftkings", -5.5), ("fanduel", -6.0), ("pinnacle", -5.5),
                                                    ("twsport", -5.5)]
    homes = {m.bookmaker: by_side(m)["home"] for m in out}
    assert homes["pinnacle"].fair_no_vig_prob == pytest.approx((1 / 1.95) / (1 / 1.95 + 1 / 1.93))
    assert homes["draftkings"].fair_no_vig_prob == pytest.approx(0.5)
    assert homes["fanduel"].push_prob == 0.03 and homes["pinnacle"].push_prob == 0.0
    assert homes["twsport"].ev_per_unit == pytest.approx(0.52 * 1.75 - 1)
    assert homes["pinnacle"].ev_per_unit == pytest.approx(0.52 * 1.95 - 1)
    assert len({id(m) for m in out}) == 4 and all(m.status == PRICED for m in out)       # 不平均、不挑最佳


# ======================= Time alignment ======================= #

def _pred_row(pid, created, kind="early", mv="ml-v2.0", av=ART, as_of=None, margin=3.0):
    return {"id": pid, "game_id": 7, "model_version": mv, "created_at": created, "home_win_prob": 0.6,
            "pred_margin": margin, "pred_total": 224.0, "pred_home_h1": 56.0, "pred_away_h1": 54.0,
            "features_json": {"artifact_version": av, "profile": "final" if kind == "final" else "early",
                              "prediction_kind": kind, "prediction_as_of_utc": (as_of or created).isoformat()}}


def test_future_prediction_cannot_price_earlier_odds():
    later = pred(created=T0 + timedelta(minutes=1))
    with pytest.raises(FutureDataError):
        price_market(odds(ml(1.9, 1.9)), later, Stub({("margin", 0): (0.5, 0, 0.5)}), as_of=T0)
    assert select_prediction([_pred_row(1, T0 + timedelta(minutes=1))], 7, T0) is None
    # created_at 早、但輸入時點（prediction_as_of_utc）晚於 T → 也不可用
    assert select_prediction([_pred_row(2, T0 - timedelta(hours=1), as_of=T0 + timedelta(hours=1))], 7, T0) is None


def test_future_odds_cannot_be_priced_at_earlier_time():
    with pytest.raises(FutureDataError):
        price_market(odds(ml(1.9, 1.9), at=T0 + timedelta(seconds=1)), None, None, as_of=T0)


def test_later_odds_and_predictions_do_not_alter_earlier_analysis():
    rows = [_row(1, "twsport", "ml", T0 - timedelta(hours=2), home_odds=1.8, away_odds=2.0)]
    preds = [_pred_row(1, T0 - timedelta(hours=3))]
    st = Stub({("margin", 0): (0.6, 0.0, 0.4)})
    before = [m.to_dict() for m in price_game_as_of(rows, preds, 7, T0, lambda p: st)]
    rows += [_row(2, "twsport", "ml", T0 + timedelta(hours=1), home_odds=1.5, away_odds=2.6)]       # 之後的線動
    preds += [_pred_row(2, T0 + timedelta(minutes=30), kind="final")]                              # 之後的 final
    after = [m.to_dict() for m in price_game_as_of(rows, preds, 7, T0, lambda p: st)]
    assert before == after and after[0]["odds_snapshot_id"] == 1 and after[0]["prediction_id"] == 1
    later = price_game_as_of(rows, preds, 7, T0 + timedelta(hours=2), lambda p: st)
    assert later[0].odds_snapshot_id == 2 and later[0].prediction_id == 2


def test_latest_valid_prediction_at_analysis_time_is_selected():
    rows = [_pred_row(1, TIP - timedelta(hours=20), "early"),
            _pred_row(2, TIP - timedelta(hours=5), "refresh"),
            _pred_row(3, TIP - timedelta(hours=1), "final"),
            _pred_row(4, TIP - timedelta(hours=4), "early", mv="ml-v1.0"),                 # 無效：舊模型
            _pred_row(5, TIP - timedelta(hours=3, minutes=30), "early", av=None)]           # 無效：無 artifact
    assert select_prediction(rows, 7, TIP - timedelta(hours=3)).prediction_id == 2
    assert select_prediction(rows, 7, TIP - timedelta(minutes=10)).prediction_id == 3
    assert select_prediction(rows, 7, TIP - timedelta(hours=10)).prediction_id == 1
    assert select_prediction(rows, 7, TIP - timedelta(hours=30)) is None
    assert select_prediction(rows, 8, TIP) is None


def test_analysis_as_of_is_deterministic_effective_time():
    p = pred(created=T0 - timedelta(hours=3))
    a = price_market(odds(ml(1.9, 1.9), at=T0 - timedelta(hours=1)), p, Stub({("margin", 0): (0.5, 0, 0.5)}),
                     as_of=T0)
    b = price_market(odds(ml(1.9, 1.9), at=T0 - timedelta(hours=1)), p, Stub({("margin", 0): (0.5, 0, 0.5)}),
                     as_of=T0 + timedelta(days=3))
    assert a.analysis_as_of == b.analysis_as_of == T0 - timedelta(hours=1)
    assert a.to_dict() == b.to_dict()


def test_artifact_version_mismatch_is_refused():
    with pytest.raises(ModelProbabilityError):
        price_market(odds(ml(1.9, 1.9)), pred(av="A"), Stub({("margin", 0): (0.5, 0, 0.5)}, artifact_version="B"),
                     as_of=T0)


# ======================= Diagnostics ======================= #

def test_logistic_vs_margin_derived_consistency_warning():
    st = Stub({("margin", 0): (0.62, 0.0, 0.38)})
    far = price_market(odds(ml(1.6, 2.4)), pred(hw=0.70), st, as_of=T0)
    near = price_market(odds(ml(1.6, 2.4)), pred(hw=0.66), st, as_of=T0)
    assert far.diagnostics["ml_consistency"]["warning"] is True and "ml_logistic_margin_divergence" in far.warnings
    assert far.diagnostics["ml_consistency"]["abs_diff"] == pytest.approx(0.08)
    assert near.diagnostics["ml_consistency"]["warning"] is False and near.warnings == []
    assert by_side(far)["home"].model_prob == by_side(near)["home"].model_prob == 0.62        # 不改模型機率


def test_high_vig_market_is_priced_with_warning():
    mp = price_market(odds(ml(1.5, 1.5)), pred(), Stub({("margin", 0): (0.5, 0, 0.5)}), as_of=T0)
    assert mp.status == PRICED and "overround_high" in mp.warnings


# ======================= Serialization ======================= #

def test_serialization_is_json_safe_and_rows_cover_schema():
    mp = price_market(odds(snap(SPREAD, home_line=-5.0, prices={"home": 1.91, "away": 1.91})), pred(),
                      Stub({("margin", 5.0): (0.47, 0.06, 0.47)}), as_of=T0)
    json.dumps(mp.to_dict(), allow_nan=False)
    rows = mp.to_rows()
    assert len(rows) == 2 and all(set(COLUMNS) <= set(r) for r in rows)
    assert {r["side"] for r in rows} == {"home", "away"} and rows[0]["odds_snapshot_id"] == 10


def test_legacy_rows_without_d1_columns_are_normalized_via_canonical():
    r = {"id": 5, "game_id": 7, "source": "twsport", "market": "spread", "fetched_at": T0, "line": -3.5,
         "home_odds": 1.87, "away_odds": 1.87}
    oi = odds_input_from_row(r)
    assert oi.snapshot.model_threshold == 3.5 and oi.snapshot.away_line == 3.5 and oi.bookmaker == "twsport"
    bad = {**r, "market_type": "spread", "period": "full_game", "away_line": 3.5, "model_threshold": -3.5}
    assert odds_input_from_row(bad).invalid_reason == "threshold_mismatch"
