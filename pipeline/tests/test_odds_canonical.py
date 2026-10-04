"""D.1 canonical market model：正負號、門檻、賠率、驗證、suspended（純邏輯）"""
import math

import pytest

from core.odds.canonical import (
    CLOSED, FULL_GAME, H1, MONEYLINE, OPEN, SPREAD, SUSPENDED, THREE_WAY, TOTAL, InvalidMarket,
    away_spread_threshold, canonical_market_key, content_hash, decimal_from_american, decimal_from_fraction,
    home_spread_threshold, legacy_market, make_snapshot, model_target, validate_decimal,
)


def snap(market_type=SPREAD, period=FULL_GAME, status=OPEN, prices=None, **kw):
    prices = prices or ({"over": 1.9, "under": 1.9} if market_type == TOTAL else {"home": 1.9, "away": 1.9})
    return make_snapshot(source="twsport", bookmaker="twsport", source_event_id="E1", market_type=market_type,
                         period=period, status=status, prices=prices, **kw)


def quotes(s):
    return {q.side: q for q in s.quotes()}


# ---- spread 正負號 ---------------------------------------------------------- #

def test_home_spread_sign_conversion():
    """Home −5.5：主隊過盤 ⇔ margin > +5.5。"""
    assert home_spread_threshold(-5.5) == 5.5
    assert home_spread_threshold(+3.5) == -3.5
    s = snap(home_line=-5.5)
    q = quotes(s)["home"]
    assert (q.display_line, q.model_threshold, q.comparator, q.model_target) == (-5.5, 5.5, "gt", "margin")
    assert s.line == -5.5 and s.model_threshold == 5.5


def test_away_spread_sign_conversion_is_symmetric():
    """Away +5.5：客隊過盤 ⇔ margin < +5.5；Away −3.5（客隊讓分）⇔ margin < −3.5。"""
    assert away_spread_threshold(5.5) == 5.5
    assert away_spread_threshold(-3.5) == -3.5
    s = snap(home_line=-5.5)
    q = quotes(s)["away"]
    assert (q.display_line, q.model_threshold, q.comparator) == (5.5, 5.5, "lt")
    s2 = snap(home_line=3.5)                     # 主隊受讓 → 客隊讓 3.5
    assert quotes(s2)["away"].display_line == -3.5 and quotes(s2)["away"].model_threshold == -3.5
    assert quotes(s2)["home"].model_threshold == -3.5


@pytest.mark.parametrize("h", [-12.5, -5.5, -0.5, 0.0, 0.5, 7.0, 15.5])
def test_home_and_away_cover_regions_partition_margin(h):
    """任一 margin（非 push）恰好落在一邊：主隊過盤 ⇔ margin + h > 0；客隊 ⇔ −margin − h > 0。"""
    s = snap(home_line=h)
    q = quotes(s)
    for margin in range(-30, 31):
        home_cover = margin + h > 0
        away_cover = -margin + (-h) > 0
        assert home_cover == (margin > q["home"].model_threshold)
        assert away_cover == (margin < q["away"].model_threshold)


def test_spread_from_away_line_only_and_asymmetric_rejected():
    s = snap(away_line=4.5)
    assert s.line == -4.5 and s.away_line == 4.5 and s.model_threshold == 4.5
    with pytest.raises(InvalidMarket) as e:
        snap(home_line=-5.5, away_line=6.5)
    assert e.value.reason == "asymmetric_spread"


# ---- total / moneyline / H1 ------------------------------------------------- #

def test_total_over_under():
    s = snap(TOTAL, total_line=225.5)
    q = quotes(s)
    assert (q["over"].model_threshold, q["over"].comparator, q["over"].model_target) == (225.5, "gt", "total")
    assert (q["under"].model_threshold, q["under"].comparator) == (225.5, "lt")
    assert s.market == "total" and s.canonical_key == "total"


def test_moneyline_threshold_zero():
    s = snap(MONEYLINE)
    q = quotes(s)
    assert s.line is None and q["home"].model_threshold == 0 and q["home"].comparator == "gt"
    assert q["away"].comparator == "lt" and s.market == "ml" and s.canonical_key == "moneyline"


def test_h1_markets_use_h1_targets():
    assert model_target(SPREAD, H1) == "h1_margin" and model_target(TOTAL, H1) == "h1_total"
    assert model_target(MONEYLINE, H1) == "h1_margin"
    assert [legacy_market(t, H1) for t in (MONEYLINE, SPREAD, TOTAL)] == ["h1_ml", "h1_spread", "h1_total"]
    assert [canonical_market_key(t, H1) for t in (MONEYLINE, SPREAD, TOTAL)] == ["h1_moneyline", "h1_spread", "h1_total"]
    s = snap(SPREAD, H1, home_line=-2.5)
    assert s.model_target == "h1_margin" and s.model_threshold == 2.5 and s.market == "h1_spread"
    t = snap(TOTAL, H1, total_line=112.5)
    assert t.model_target == "h1_total" and t.model_threshold == 112.5


def test_h1_three_way_moneyline_has_draw_eq_zero():
    s = snap(MONEYLINE, H1, prices={"home": 1.7, "away": 1.7, "draw": 10.0}, outcome_set=THREE_WAY)
    q = quotes(s)
    assert q["draw"].comparator == "eq" and q["draw"].model_threshold == 0 and q["draw"].price == 10.0
    with pytest.raises(InvalidMarket):                 # 三向缺和局
        snap(MONEYLINE, H1, prices={"home": 1.7, "away": 1.7}, outcome_set=THREE_WAY)
    with pytest.raises(InvalidMarket) as e:            # 兩向卻多出和局
        snap(MONEYLINE, prices={"home": 1.7, "away": 1.7, "draw": 10.0})
    assert e.value.reason == "unexpected_outcome"


# ---- 賠率 ------------------------------------------------------------------- #

def test_decimal_conversions_and_validation():
    assert decimal_from_fraction("13", "25") == 1.52
    assert decimal_from_fraction(3, 4) == 1.75
    assert decimal_from_fraction(1, 3) == 1.3333
    assert decimal_from_american(150) == 2.5 and decimal_from_american(-200) == 1.5
    for bad in (0, 1.0, -2, float("nan"), float("inf"), None, "x", 5000):
        with pytest.raises(InvalidMarket):
            validate_decimal(bad)
    for up, down in ((0, 5), (3, 0), ("", "4"), (None, None)):
        with pytest.raises(InvalidMarket):
            decimal_from_fraction(up, down)


@pytest.mark.parametrize("prices,reason", [
    ({"home": 1.9}, "missing_side"),
    ({"home": 1.9, "away": float("nan")}, "price_nan"),
    ({"home": 1.9, "away": 0}, "price_out_of_range"),
    ({"home": 1.9, "away": 1.0}, "price_out_of_range"),
])
def test_open_market_requires_valid_prices_on_all_sides(prices, reason):
    with pytest.raises(InvalidMarket) as e:
        snap(MONEYLINE, prices=prices)
    assert e.value.reason == reason


@pytest.mark.parametrize("kw,reason", [
    (dict(market_type=SPREAD, home_line=float("nan")), "line_nan"),
    (dict(market_type=SPREAD, home_line=-55.5), "line_out_of_range"),
    (dict(market_type=SPREAD, home_line=-5.3), "line_not_quarter_multiple"),
    (dict(market_type=TOTAL, total_line=45.5), "line_out_of_range"),
    (dict(market_type=TOTAL), "missing_line"),
    (dict(market_type=SPREAD), "missing_line"),
])
def test_unreasonable_lines_rejected(kw, reason):
    with pytest.raises(InvalidMarket) as e:
        snap(**kw)
    assert e.value.reason == reason


def test_suspended_market_keeps_status_and_never_looks_active():
    s = snap(MONEYLINE, status=SUSPENDED, prices={"home": 1.9, "away": None})
    assert s.status == SUSPENDED and s.away_odds is None and s.home_odds == 1.9
    c = snap(MONEYLINE, status=CLOSED, prices={"home": 0, "away": float("nan")})
    assert c.status == CLOSED and c.home_odds is None and c.away_odds is None
    o = snap(MONEYLINE, prices={"home": 1.9, "away": 1.9})
    assert content_hash(1, s) != content_hash(1, o)      # 狀態改變 = 實質變化


def test_content_hash_semantics():
    a = snap(home_line=-5.5, prices={"home": 1.9, "away": 1.9})
    assert content_hash(1, a) == content_hash(1, snap(home_line=-5.5, prices={"home": 1.90000001, "away": 1.9}))
    assert content_hash(1, a) != content_hash(1, snap(home_line=-6.5, prices={"home": 1.9, "away": 1.9}))  # 線動
    assert content_hash(1, a) != content_hash(1, snap(home_line=-5.5, prices={"home": 1.85, "away": 1.95}))  # 只動價
    assert content_hash(1, a) != content_hash(2, a)
    # 來源更新時間 / raw 不影響內容身分
    from datetime import datetime, timezone
    b = make_snapshot(source="twsport", bookmaker="twsport", source_event_id="E1", market_type=SPREAD,
                      period=FULL_GAME, status=OPEN, prices={"home": 1.9, "away": 1.9}, home_line=-5.5,
                      source_updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc), raw={"x": 1})
    assert content_hash(1, a) == content_hash(1, b)
    assert not math.isnan(a.model_threshold)
