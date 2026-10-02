"""C.5C 建模：收縮/先驗、walk-forward 切分、訓練集補值、特徵組消融可重現、指標與 bootstrap"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from core.jobs import c5c_evaluate as ev
from core.jobs.c5c_inputs import C5CInputs
from core.models import temporal_features as tf
from core.models import temporal_model as tm
from core.models.ml_features import build_features
from core.models.pregame_features import GameRecord
from core.timeutil import UTC

SEASONS = ["2021-22", "2022-23", "2023-24", "2024-25"]
N_TEAMS = 6


def synthetic_inputs(seed=3, games_per_season=150):
    """6 隊、4 季；每季實力重抽一部分（模擬名單變動），比分 = 實力差 + 雜訊。"""
    rng = np.random.default_rng(seed)
    strength = rng.normal(0, 5, N_TEAMS)
    games, derived, players, rows = [], {}, {}, []
    gid = 0
    for si, season in enumerate(SEASONS):
        strength = 0.6 * strength + rng.normal(0, 3, N_TEAMS)
        start = datetime(2021 + si, 10, 20, 23, 0, tzinfo=UTC)
        for i in range(games_per_season):
            h, a = rng.choice(N_TEAMS, 2, replace=False) + 1
            gid += 1
            tip = start + timedelta(hours=12 * i)
            m = strength[h - 1] - strength[a - 1] + 2.5 + rng.normal(0, 12)
            tot = 222 + rng.normal(0, 18)
            hp, ap = int(round((tot + m) / 2)), int(round((tot - m) / 2))
            if hp == ap:
                hp += 1
            h1h, h1a = hp // 2, ap // 2
            games.append(GameRecord(gid, season, tip, int(h), int(a), f"T{h}", f"T{a}", hp, ap, h1h, h1a))
            for tid, pts, opp in ((h, hp, ap), (a, ap, hp)):
                poss = 99 + rng.normal(0, 3)
                derived[(gid, int(tid))] = {"pace": poss, "ortg": 100 * pts / poss, "drtg": 100 * opp / poss,
                                            "net_rtg": 100 * (pts - opp) / poss, "efg_pct": 0.54, "ts_pct": 0.58,
                                            "tov_pct": 0.13, "orb_pct": 0.25, "drb_pct": 0.75, "ftr": 0.2,
                                            "fg3a_rate": 0.4}
            players[gid] = [(int(t), int(t) * 100 + k + (si * 10 if k == 0 else 0), float(48 - 4 * k), k < 5, 0.0)
                            for t in (h, a) for k in range(9)]
            rows.append({"game_id": gid, "season": season, "season_stage": "regular", "date_utc": tip,
                         "home_team_id": int(h), "away_team_id": int(a), "home_pts": hp, "away_pts": ap,
                         "home_h1": h1h, "away_h1": h1a,
                         "elo_home": 1500 + 25 * strength[h - 1], "elo_away": 1500 + 25 * strength[a - 1]})
    df = pd.DataFrame(rows)
    df["date_utc"] = pd.to_datetime(df["date_utc"], utc=True)
    elo = df[["game_id", "elo_home", "elo_away"]].astype(float)
    elo["game_id"] = elo["game_id"].astype(int)
    return C5CInputs(games, derived, players, tf.InjuryIndex([]), elo,
                     pd.DataFrame({"game_id": pd.Series(dtype=int), "elo_p": pd.Series(dtype=float)}),
                     build_features(df))


@pytest.fixture(scope="module")
def full():
    inp = synthetic_inputs()
    df = tf.build_temporal_features(inp.games, inp.derived, inp.players, inp.injury_index)
    return ev.assemble(df, inp)


# ---------------- walk-forward 切分 ---------------- #

def test_prepare_fold_uses_only_earlier_seasons_for_training(full):
    tr, te, _ = ev.prepare_fold(full, "2023-24")
    assert set(tr["season"]) == {"2021-22", "2022-23"} and set(te["season"]) == {"2023-24"}
    assert tr["game_time_utc"].max() < te["game_time_utc"].min()


def test_test_season_labels_do_not_affect_blend_params_fills_or_test_features(full):
    tr, te, params = ev.prepare_fold(full, "2023-24")
    pert = full.copy()
    m = pert["season"] >= "2023-24"
    for c in [c for c in pert.columns if c.startswith("y_")]:
        pert.loc[m, c] = pert.loc[m, c] * 3 + 7
    tr2, te2, params2 = ev.prepare_fold(pert, "2023-24")
    assert {k: v.__dict__ for k, v in params.items()} == {k: v.__dict__ for k, v in params2.items()}
    feats = sorted({c for g in tm.TARGETS.values() for cols in g[1].values() for c in cols})
    pd.testing.assert_frame_equal(te[feats].reset_index(drop=True), te2[feats].reset_index(drop=True))


def test_fill_constants_come_from_training_frame_only(full):
    tr, te, params = ev.prepare_fold(full, "2023-24")
    _, fills = tm.add_model_columns(tm.apply_blends(full[full["season"] < "2023-24"], params))
    te_a, _ = tm.add_model_columns(tm.apply_blends(full[full["season"] == "2023-24"], params), fills)
    shifted = full[full["season"] == "2023-24"].copy()
    te_b, fills_b = tm.add_model_columns(tm.apply_blends(shifted, params), fills)
    assert fills_b == fills                                                    # 測試集不會新增/改變補值常數
    pd.testing.assert_frame_equal(te_a, te_b)


# ---------------- 收縮 / 先驗 ---------------- #

def _lf(gp, x, P, L=0.0, c=np.nan, rp=np.nan):
    n = len(gp)
    return pd.DataFrame({"gp": np.asarray(gp, float), "x": np.asarray(x, float), "P": np.full(n, P, float),
                         "L": np.full(n, L, float), "c": np.full(n, c, float), "rp": np.full(n, rp, float)})


def test_prior_influence_declines_as_current_sample_grows():
    bp = tm.BlendParams("est_net_rtg", "blend", k=10.0, a=0.2, b=0.4, c_bar=0.75)
    gp = np.arange(0, 82)
    est = tm.predict_blend(_lf(gp, np.where(gp > 0, 0.0, np.nan), P=10.0, c=0.75), bp)   # 本季平均 0、上季 +10
    assert est[0] == pytest.approx((0.2 + 0.4 * 0.75) * 10.0)                  # 第 1 場前：只有先驗
    assert np.all(np.diff(est) < 0)                                              # 先驗影響單調下降
    assert est[-1] < 0.15 * est[0]


def test_gp0_uses_training_mean_continuity_and_no_prev_falls_back_to_league():
    bp = tm.BlendParams("margin", "blend", k=10.0, a=0.1, b=0.5, c_bar=0.8, l_fallback=1.0)
    lf = _lf([0], [np.nan], P=6.0, L=np.nan)
    assert tm.predict_blend(lf, bp)[0] == pytest.approx(1.0 + (0.1 + 0.5 * 0.8) * (6.0 - 1.0))
    lf2 = _lf([0, 5], [np.nan, 3.0], P=np.nan, L=2.0)
    est = tm.predict_blend(lf2, bp)
    assert est[0] == pytest.approx(2.0) and est[1] == pytest.approx(2.0 + 5 / 15 * 1.0)


def test_higher_continuity_gives_prior_more_weight_when_b_positive():
    bp = tm.BlendParams("est_net_rtg", "blend", k=10.0, a=0.1, b=0.5, c_bar=0.75)
    low = tm.predict_blend(_lf([3], [0.0], P=8.0, c=0.3), bp)[0]
    high = tm.predict_blend(_lf([3], [0.0], P=8.0, c=0.95), bp)[0]
    assert high > low > 0


def test_empirical_shrinkage_recovers_noise_to_signal_ratio():
    """常態-常態模型：真實 k = σ²/τ² = 144/16 = 9；擬合的 k 應落在附近（格點）。"""
    rng = np.random.default_rng(0)
    rows = []
    for team in range(400):
        theta = rng.normal(0, 4)
        ys = theta + rng.normal(0, 12, 40)
        for n in range(40):
            rows.append({"game_id": team * 100 + n, "season": "2022-23", "home_gp": n,
                         "home_margin_season": ys[:n].mean() if n else np.nan, "home_margin_prev": np.nan,
                         "league_margin_prev": 0.0, "home_ret_min_pct": np.nan, "home_roster_prior_pm48": np.nan,
                         "y_home_margin": ys[n],
                         "away_gp": 0, "away_margin_season": np.nan, "away_margin_prev": np.nan,
                         "away_ret_min_pct": np.nan, "away_roster_prior_pm48": np.nan, "y_away_margin": np.nan})
    bp = tm.fit_blend(pd.DataFrame(rows), "margin", "current")
    assert 6 <= bp.k <= 15


def test_fixed_weight_blend_ignores_sample_size():
    lf = _lf([1, 40], [10.0, 10.0], P=0.0)
    est = tm.fixed_weight_blend(lf, 0.7, 0.0)
    assert est[0] == pytest.approx(3.0) and est[1] == pytest.approx(3.0)


# ---------------- 特徵組 / 消融可重現 ---------------- #

def test_ablation_groups_are_cumulative():
    for groups in (tm.WIN_GROUPS, tm.MARGIN_GROUPS, tm.TOTAL_GROUPS, tm.H1_TOTAL_GROUPS):
        assert set(groups["B"]) <= set(groups["C"])
        assert set(groups["D"]) <= set(groups["E"]) and set(groups["D"]) <= set(groups["E_count"])
        assert not any("inj_" in c for c in groups["D"]) and any("inj_" in c for c in groups["E"])
        assert not any(("blend" in c or "continuity" in c or "ret_min" in c) for c in groups["C"])
    assert set(tm.WIN_GROUPS["B"]) <= set(tm.WIN_GROUPS["C"])
    assert tm.WIN_GROUPS["A"] == ["elo_diff"]


def test_ablation_is_reproducible(full):
    val = {t: {g: {"family": "linear", "hp": 1.0 if t == "win" else 10.0, "train_start": "2021-22",
                   "metrics": {ev.primary(t): 0.0}}
               for g in ("A", "B", "C", "D", "E")} for t in tm.TARGETS}
    # include_refs=False：Phase C 參考模型需要 XGBoost（macOS 需 libomp），消融本身只用線性模型
    a = ev.evaluate(full, val, seasons=["2024-25"], include_refs=False)
    b = ev.evaluate(full, val, seasons=["2024-25"], include_refs=False)
    pd.testing.assert_frame_equal(a, b)
    assert set(a["model"]) == {"A", "B", "C", "D", "E"}
    win = a[a["target"] == "win"]
    assert win["pred"].between(0, 1).all()


def test_xgb_candidate_is_not_evaluated_unless_selected_on_validation(full):
    val = {t: {"E": {"family": "linear", "hp": 1.0, "train_start": "2021-22", "metrics": {ev.primary(t): 0.5}},
               "E_xgb": {"family": "xgb", "hp": {"n_estimators": 5}, "train_start": "2021-22",
                         "metrics": {ev.primary(t): 0.9}}} for t in tm.TARGETS}
    out = ev.evaluate(full, val, seasons=["2024-25"], include_refs=False)
    assert set(out["model"]) == {"E"}
    assert ev.choose_production(val) == {t: "E" for t in tm.TARGETS}


def test_season_game_bucket_edges():
    b = tm.season_game_bucket(pd.Series([0, 4, 5, 9, 10, 19, 20, 70])).astype(str).tolist()
    assert b == ["1-5", "1-5", "6-10", "6-10", "11-20", "11-20", "21+", "21+"]


# ---------------- 指標 ---------------- #

def test_win_metrics_and_calibration():
    p = np.array([0.9, 0.8, 0.3, 0.6])
    y = np.array([1, 1, 0, 0])
    m = tm.win_metrics(p, y)
    assert m["accuracy"] == pytest.approx(0.75)
    assert m["brier"] == pytest.approx(np.mean((p - y) ** 2))
    assert m["log_loss"] == pytest.approx(-np.mean(np.log([0.9, 0.8, 0.7, 0.4])))
    assert sum(r["n"] for r in m["reliability"]) == 4


def test_paired_bootstrap_identical_and_shifted():
    rng = np.random.default_rng(1)
    la = rng.random(500)
    blocks = np.repeat(np.arange(100), 5)
    same = tm.paired_bootstrap(la, la, blocks)
    assert same["mean_diff"] == 0 and same["ci_lo"] == 0 and same["ci_hi"] == 0
    better = tm.paired_bootstrap(la - 0.1, la, blocks)
    assert better["ci_hi"] < 0 and better["p_better"] == 1.0
    assert tm.paired_bootstrap(la - 0.1, la, blocks) == better                 # 固定種子 → 可重現
