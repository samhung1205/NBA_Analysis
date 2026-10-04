"""C.5E 預測分佈：盤口線機率的單調性 / 總和 / push、擬合可重現、覆蓋率計算、尺度模型收縮、只用過去的 OOS 殘差"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from core.models import distributions as dist
from core.timeutil import UTC


def _resid(n=4000, sd=14.0, seed=1):
    return np.random.default_rng(seed).normal(-0.3, sd, n)


@pytest.fixture(scope="module", params=["gaussian", "empirical"])
def fitted(request):
    return dist.fit_distribution("margin", _resid(), kind=request.param)


# ---------------- 機率性質 ---------------- #

def test_probability_monotone_in_line_and_sums_to_one(fitted):
    lines = np.arange(-30, 30.01, 0.25)
    res = dist.line_probabilities(fitted, 4.3, lines)
    above = [r.probability_above for r in res]
    below = [r.probability_below for r in res]
    assert all(a >= b - 1e-15 for a, b in zip(above, above[1:]))          # 線越高，P(Y > line) 越小
    assert all(a <= b + 1e-15 for a, b in zip(below, below[1:]))
    for r in res:
        assert r.probability_above + r.probability_push + r.probability_below == pytest.approx(1.0, abs=1e-12)
        assert 0 <= r.probability_push <= 1


def test_push_only_on_integer_lines_and_margin_never_pushes_at_zero():
    d = dist.fit_distribution("margin", _resid(), kind="gaussian")
    r = {x.line: x for x in dist.line_probabilities(d, 0.4, [-3.5, -3, -2.25, 0, 0.5, 3])}
    assert r[-3.5].probability_push == 0 and r[0.5].probability_push == 0 and r[-2.25].probability_push == 0
    assert r[-3].probability_push > 0.01 and r[3].probability_push > 0.01
    assert r[0].probability_push == 0.0                                    # 全場分差沒有平手（延長賽）
    assert r[0].probability_above == pytest.approx(r[0.5].probability_above + 0.0, abs=1e-12)  # P(Y>0) = P(Y≥1)
    # 上半場可以平手
    h1 = dist.fit_distribution("h1_margin", _resid(sd=11), kind="gaussian")
    z = dist.line_probabilities(h1, 0.4, [0])[0]
    assert z.probability_push > 0.02


def test_integer_line_splits_win_push_loss_consistently(fitted):
    """整數線 k：P(Y>k) + P(Y=k) = P(Y > k − 0.5)，P(Y<k) = P(Y < k − 0.5)。"""
    for k in (-7, -2, 3, 8):
        a, b = dist.line_probabilities(fitted, 2.6, [k, k - 0.5])
        assert a.probability_above + a.probability_push == pytest.approx(b.probability_above, abs=1e-12)
        assert a.probability_below == pytest.approx(b.probability_below, abs=1e-12)


def test_center_shift_moves_probabilities(fitted):
    lo, hi = dist.line_probabilities(fitted, -2.0, [0.5])[0], dist.line_probabilities(fitted, 6.0, [0.5])[0]
    assert hi.probability_above > lo.probability_above


def test_gaussian_probability_matches_closed_form():
    d = dist.FittedDistribution("total", "gaussian", mu=0.5, sigma=18.0)
    r = dist.line_probabilities(d, 225.3, [230.5])[0]
    # P(Y ≥ 231) = 1 − Φ((230.5 − 225.3 − 0.5)/18)
    assert r.probability_above == pytest.approx(1 - 0.5 * (1 + math.erf((230.5 - 225.8) / 18 / math.sqrt(2))), abs=1e-6)


def test_quarter_line_components():
    assert dist.quarter_line_components(-2.25) == (-2.5, -2.0)
    assert dist.quarter_line_components(215.75) == (215.5, 216.0)
    assert dist.quarter_line_components(-3.5) == (-3.5,)
    assert dist.quarter_line_components(4.0) == (4.0,)


def test_line_probability_is_deterministic(fitted):
    a = dist.line_probabilities(fitted, 3.21, [-5.5, 0, 2, 7.5])
    b = dist.line_probabilities(fitted, 3.21, [-5.5, 0, 2, 7.5])
    assert a == b


# ---------------- 擬合 ---------------- #

def test_fit_is_reproducible_and_state_roundtrips():
    r = _resid()
    for kind in ("gaussian", "empirical"):
        a, b = dist.fit_distribution("total", r, kind=kind), dist.fit_distribution("total", r, kind=kind)
        assert a.to_state() == b.to_state()
        c = dist.FittedDistribution.from_state(a.to_state())
        assert dist.line_probabilities(c, 222.2, [220.5, 230]) == dist.line_probabilities(a, 222.2, [220.5, 230])


def test_empirical_captures_skew_that_gaussian_misses():
    rng = np.random.default_rng(4)
    r = np.concatenate([rng.normal(0, 17, 9000), rng.normal(25, 6, 600)])       # 延長賽造成的右尾
    r = r - r.mean()
    e = dist.fit_distribution("total", r, kind="empirical")
    g = dist.fit_distribution("total", r, kind="gaussian")
    y = np.concatenate([rng.normal(0, 17, 9000), rng.normal(25, 6, 600)]) - r.mean() + 220
    pred = np.full(len(y), 220.0)
    ye = dist.score_outcomes(*dist.outcome_pmf(e, pred), np.round(y))
    yg = dist.score_outcomes(*dist.outcome_pmf(g, pred), np.round(y))
    assert ye["nll"].mean() < yg["nll"].mean()


def test_invalid_states_are_rejected():
    st = dist.fit_distribution("margin", _resid(), kind="empirical").to_state()
    with pytest.raises(ValueError, match="schema"):
        dist.FittedDistribution.from_state({**st, "schema": 99})
    with pytest.raises(ValueError, match="kind"):
        dist.FittedDistribution.from_state({**st, "kind": "student_t"})
    with pytest.raises(ValueError, match="格點"):
        dist.FittedDistribution.from_state({**st, "z_grid": {"lo": -10.0, "hi": 10.0, "n": 2401}})
    bad = list(st["z_cdf"])
    bad[100], bad[101] = 0.9, 0.1
    with pytest.raises(ValueError, match="單調"):
        dist.FittedDistribution.from_state({**st, "z_cdf": bad})


def test_scale_model_learns_abundant_signal_and_shrinks_rare_context():
    rng = np.random.default_rng(7)
    n = 6000
    early = (rng.random(n) < 0.2).astype(float)
    inj = np.zeros(n)
    inj[:5] = 1.0                                                   # 只有 5 場的情境
    sd = 14 * np.where(early > 0, 1.3, 1.0)
    r = rng.normal(0, sd)
    r[:5] *= 3.0                                                    # 那 5 場剛好很極端
    ctx = {"abs_pred": np.abs(rng.normal(0, 6, n)), "early": early, "inj_unknown": inj, "playoffs": np.zeros(n)}
    d = dist.fit_distribution("margin", r, kind="gaussian", scaled=True, ctx=ctx, lam=200.0)
    assert d.scale_model["coef"]["early"] == pytest.approx(math.log(1.3), abs=0.05)
    assert abs(d.scale_model["coef"]["inj_unknown"]) < 0.25 * abs(math.log(3.0))     # 被收縮回全域
    loose = dist.fit_distribution("margin", r, kind="gaussian", scaled=True, ctx=ctx, lam=1e-6)
    assert abs(loose.scale_model["coef"]["inj_unknown"]) > abs(d.scale_model["coef"]["inj_unknown"])
    s = d.scales({**ctx, "early": np.array([0.0, 1.0]), "abs_pred": np.array([5.0, 5.0]),
                  "inj_unknown": np.zeros(2), "playoffs": np.zeros(2)}, 2)
    assert s[1] / s[0] == pytest.approx(math.exp(d.scale_model["coef"]["early"]))


def test_scale_is_clipped():
    d = dist.FittedDistribution("margin", "gaussian", 0.0, 14.0,
                                scale_model={"features": ["early"], "intercept": math.log(14.0),
                                             "coef": {"early": 5.0}, "center": {"early": 0.0}, "scale": {"early": 1.0}})
    assert d.scales({"early": np.array([1.0])}, 1)[0] == pytest.approx(28.0)


# ---------------- 評估工具 ---------------- #

def test_coverage_and_pit_on_calibrated_simulation():
    rng = np.random.default_rng(9)
    n = 20000
    pred = rng.normal(0, 6, n)
    y = np.round(pred + rng.normal(0, 14, n))
    y[y == 0] = 1
    d = dist.FittedDistribution("margin", "gaussian", 0.0, 14.0)
    sc = dist.score_outcomes(*dist.outcome_pmf(d, pred), y)
    cov = dist.coverage(sc["pit"])
    for k, lvl in (("50%", .5), ("80%", .8), ("90%", .9), ("95%", .95)):
        assert cov[k] == pytest.approx(lvl, abs=0.015)
    assert dist.pit_histogram(sc["pit"])["max_abs_dev"] < 0.01
    # 過窄的分佈 → 覆蓋率不足
    narrow = dist.FittedDistribution("margin", "gaussian", 0.0, 9.0)
    assert dist.coverage(dist.score_outcomes(*dist.outcome_pmf(narrow, pred), y)["pit"])["90%"] < 0.8


def test_coverage_counts_exactly():
    pit = np.array([0.01, 0.2, 0.26, 0.5, 0.74, 0.76, 0.99, 0.5])
    assert dist.coverage(pit, [0.5])["50%"] == pytest.approx(4 / 8)


def test_central_interval_matches_coverage_definition():
    d = dist.FittedDistribution("total", "gaussian", 0.0, 18.0)
    lo, hi = dist.central_interval(d, 220.0, 0.8)
    assert lo < 220 < hi and (hi - lo) == pytest.approx(2 * 1.2816 * 18, abs=2)


def test_rps_prefers_sharper_correct_distribution():
    rng = np.random.default_rng(2)
    y = np.round(rng.normal(220, 15, 4000))
    pred = np.full(len(y), 220.0)
    good = dist.score_outcomes(*dist.outcome_pmf(dist.FittedDistribution("total", "gaussian", 0, 15.0), pred), y)
    wide = dist.score_outcomes(*dist.outcome_pmf(dist.FittedDistribution("total", "gaussian", 0, 30.0), pred), y)
    assert good["rps"].mean() < wide["rps"].mean() and good["nll"].mean() < wide["nll"].mean()


def test_probability_matrix_matches_single_game_function(fitted):
    pred = np.array([3.2, -6.7])
    lines = np.array([[0.5, 3.0, -2.5], [-6.0, -6.5, 1.0]])
    s, p = dist.outcome_pmf(fitted, pred)
    above, push = dist.probability_above_matrix(s, p, lines)
    for i in range(2):
        ref = dist.line_probabilities(fitted, pred[i], lines[i])
        assert np.allclose(above[i], [r.probability_above for r in ref], atol=1e-12)
        assert np.allclose(push[i], [r.probability_push for r in ref], atol=1e-12)


def test_reliability_bins():
    rel = dist.reliability(np.array([0.1, 0.12, 0.9, 0.88]), np.array([0, 0, 1, 1]), [0, 0.5, 1.0])
    assert rel["bins"][0]["observed"] == 0 and rel["bins"][1]["observed"] == 1


# ---------------- 只用過去的 OOS 殘差 ---------------- #

def _oos_frame(seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    t0 = datetime(2022, 10, 20, 23, tzinfo=UTC)
    for si, season in enumerate(["2022-23", "2023-24"]):
        for i in range(400):
            t = t0 + timedelta(days=365 * si, hours=12 * i)
            pred = rng.normal(0, 6)
            y = float(np.round(pred + rng.normal(0, 14)) or 1)
            for prof in ("early", "final"):
                rows.append({"profile": prof, "target": "margin", "season": season, "game_id": si * 1000 + i,
                             "game_time_utc": t, "pred": pred, "y": y, "resid": y - pred, "min_gp": i // 2,
                             "inj_both_known": 1.0, "is_playoffs": 0.0})
    df = pd.DataFrame(rows)
    df["game_time_utc"] = pd.to_datetime(df["game_time_utc"], utc=True)
    return df


def test_walk_forward_fit_uses_only_earlier_weeks():
    from core.jobs import c5e_evaluate as ev
    df = _oos_frame()
    base = ev.walk_forward_scores(df, "margin", "final", "2023-24", "empirical", True, 200.0)
    # 竄改：測試賽季第 200 場之後的殘差全部放大 → 之前的週的評分不可改變
    cut = df[(df.season == "2023-24")]["game_time_utc"].sort_values().iloc[400]
    tampered = df.copy()
    late = tampered["game_time_utc"] >= cut
    tampered.loc[late, "y"] = tampered.loc[late, "y"] * 5
    tampered.loc[late, "resid"] = tampered.loc[late, "y"] - tampered.loc[late, "pred"]
    again = ev.walk_forward_scores(tampered, "margin", "final", "2023-24", "empirical", True, 200.0)
    wk = ev.week_start(pd.Series([cut]))[0]
    early_rows = base["game_time_utc"] < wk
    assert early_rows.sum() > 50
    pd.testing.assert_series_equal(base.loc[early_rows, "rps"], again.loc[early_rows, "rps"])
    pd.testing.assert_series_equal(base.loc[early_rows, "sigma"], again.loc[early_rows, "sigma"])
    assert (base["n_fit"].diff().fillna(0) >= 0).all()                 # 擬合集只會隨時間擴大
