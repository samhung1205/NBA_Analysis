"""衍生進階指標公式（box-v1）"""
import pytest

from core import metrics

# 2021-10-19 MIL(主) vs BKN(客)：cdn.nba.com box score 實際數字。
# CDN 官方回傳的 MIL trueShootingPercentage=0.5623450、fieldGoalsEffectiveAdjusted=0.5380952，可交叉驗證。
MIL = dict(pts=127, fgm=48, fga=105, fg3m=17, fg3a=45, ftm=14, fta=18, oreb=20, dreb=42, tov=8, ast=25, team_min=240.0)
BKN = dict(pts=104, fgm=37, fga=84, fg3m=17, fg3a=32, ftm=13, fta=23, oreb=15, dreb=41, tov=13, ast=19, team_min=240.0)


def test_efg_and_ts_match_official_cdn_values():
    h, _ = metrics.derive_game(MIL, BKN)
    assert h["efg_pct"] == pytest.approx(0.538095238, abs=1e-8)       # (48 + 0.5*17) / 105
    assert h["ts_pct"] == pytest.approx(0.562345023, abs=1e-8)        # 127 / (2*(105 + 0.44*18))


def test_possessions_basketball_reference_formula_by_hand():
    # MIL: 105 + 0.4*18 - 1.07*(20/(20+41))*(105-48) + 8
    mil = 105 + 7.2 - 1.07 * (20 / 61) * 57 + 8
    # BKN: 84 + 0.4*23 - 1.07*(15/(15+42))*(84-37) + 13
    bkn = 84 + 9.2 - 1.07 * (15 / 57) * 47 + 13
    assert metrics.team_possessions(MIL, BKN) == pytest.approx(mil)
    assert metrics.team_possessions(BKN, MIL) == pytest.approx(bkn)
    assert metrics.game_possessions(MIL, BKN) == pytest.approx((mil + bkn) / 2)
    assert metrics.game_possessions(MIL, BKN) == pytest.approx(metrics.game_possessions(BKN, MIL))


def test_ratings_pace_and_net():
    h, a = metrics.derive_game(MIL, BKN)
    poss = metrics.game_possessions(MIL, BKN)
    assert h["poss"] == a["poss"] == pytest.approx(poss)
    assert h["ortg"] == pytest.approx(100 * 127 / poss) and h["drtg"] == pytest.approx(100 * 104 / poss)
    assert a["ortg"] == pytest.approx(h["drtg"]) and a["drtg"] == pytest.approx(h["ortg"])   # 對稱
    assert h["net_rtg"] == pytest.approx(-a["net_rtg"])
    assert h["pace"] == pytest.approx(poss)              # 240 分鐘 → pace = poss
    assert 90 < h["pace"] < 110 and 90 < h["ortg"] < 140  # 合理範圍


def test_overtime_pace_is_normalised_by_minutes():
    ot_h, ot_a = {**MIL, "team_min": 265.0}, {**BKN, "team_min": 265.0}   # 1 節 OT
    h, _ = metrics.derive_game(ot_h, ot_a)
    assert h["pace"] == pytest.approx(48 * h["poss"] / (265 / 5))
    assert h["pace"] < metrics.derive_game(MIL, BKN)[0]["pace"]
    assert h["ortg"] == pytest.approx(100 * 127 / h["poss"])             # ORtg 以每百回合計，不隨 OT 縮放


def test_other_rates():
    h, a = metrics.derive_game(MIL, BKN)
    assert h["tov_pct"] == pytest.approx(8 / (105 + 0.44 * 18 + 8))
    assert h["orb_pct"] == pytest.approx(20 / (20 + 41)) and h["drb_pct"] == pytest.approx(42 / (42 + 15))
    assert h["orb_pct"] + a["drb_pct"] == pytest.approx(1.0)             # 一方的進攻籃板率 + 對方防守籃板率 = 1
    assert h["ftr"] == pytest.approx(18 / 105) and h["fg3a_rate"] == pytest.approx(45 / 105)
    assert h["ast_pct"] == pytest.approx(25 / 48)


def test_missing_raw_never_fabricates():
    partial = {k: v for k, v in MIL.items() if k != "fga"}
    h, a = metrics.derive_game(partial, BKN)
    assert all(v is None for v in h.values()) and all(v is None for v in a.values())
    assert not metrics.has_raw(partial) and metrics.has_raw(BKN)


def test_zero_denominators_return_none_not_error():
    zero = dict(pts=0, fgm=0, fga=0, fg3m=0, fg3a=0, ftm=0, fta=0, oreb=0, dreb=0, tov=0, ast=0, team_min=240.0)
    h, _ = metrics.derive_game(zero, zero)
    assert h["efg_pct"] is None and h["ts_pct"] is None and h["ortg"] is None and h["ftr"] is None


def test_formula_version_is_recorded():
    assert metrics.FORMULA_VERSION == "box-v1"
