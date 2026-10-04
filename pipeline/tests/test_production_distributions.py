"""C.5E production：artifact 內的預測分佈、只用樣本外殘差、schema 相容性、early/final、資料品質旗標、決定性"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np
import pytest

from core.models import distributions as dist
from core.production import artifact as art_mod
from core.production import distribution_fit, inference, oos, probability, retrain, spec
from prod_fixtures import last_tip, scheduled, synthetic_league


@pytest.fixture(scope="module")
def league():
    return synthetic_league()


@pytest.fixture(scope="module")
def trained(league):
    cut = last_tip(league) + timedelta(hours=5)
    hist = league.before(cut - spec.TRAINING_GAME_BUFFER)
    b, f = retrain.train_bundle(hist, cut, trained_at=datetime(2026, 10, 4, tzinfo=cut.tzinfo))
    return b, f, cut, hist


@pytest.fixture()
def art(trained, tmp_path):
    b, f, _, _ = trained
    art_mod.save_version(b, tmp_path, gates=retrain.run_gates(b, f), verify=retrain.roundtrip_verifier(b, f))
    art_mod.promote(b["artifact_version"], tmp_path)
    return art_mod.load_current(tmp_path)


def _game_pred(art, league, profile="final", flags_extra=None):
    tip = last_tip(league) + timedelta(days=1)
    as_of = tip - (timedelta(hours=1) if profile == "final" else timedelta(hours=20))
    p = inference.predict_games(art, league, [scheduled(9001, tip, 1, 2)], [], as_of, kind="early")[0][0]
    assert p.profile == profile
    return p


# ---------------- artifact ---------------- #

def test_bundle_contains_distributions_and_roundtrips(trained, art):
    b, _, cut, _ = trained
    assert b["schema_version"] == 2
    for prof in spec.PROFILES:
        for t in spec.DISTRIBUTION_TARGETS:
            dd = art.profile(prof)["distributions"][t]
            st = dd["state"]
            assert dd["distribution_version"] == spec.DISTRIBUTION_VERSION
            assert st["kind"] == spec.DISTRIBUTION_SPEC[t]["kind"] and st["n_fit"] >= spec.MIN_DISTRIBUTION_FIT
            assert st["fit_end_utc"] < cut.isoformat() and dd["oos_seasons"]
            a = dist.FittedDistribution.from_state(b["profiles"][prof]["distributions"][t]["state"])
            loaded = art.distribution(prof, t)
            lines = [-10.5, -3, 0, 2.5, 7, 215.5, 230]
            assert dist.line_probabilities(a, 4.4, lines) == dist.line_probabilities(loaded, 4.4, lines)
    m = art.manifest["metadata"]["distributions"]
    assert m["version"] == spec.DISTRIBUTION_VERSION and m["oos_rows"] > 0


@pytest.mark.parametrize("mutate,match", [
    (lambda b: b.update(schema_version=1), "schema 1"),
    (lambda b: b["profiles"]["final"].pop("distributions"), "預測分佈"),
    (lambda b: b["profiles"]["early"]["distributions"].pop("total"), "total"),
    (lambda b: b["profiles"]["final"]["distributions"]["margin"].update(distribution_version="dist-v0"), "分佈版本"),
    (lambda b: b["profiles"]["final"]["distributions"]["margin"]["state"].update(schema=9), "預測分佈狀態"),
    (lambda b: b["profiles"]["final"]["distributions"]["margin"]["state"].update(kind="student_t"), "預測分佈狀態"),
    (lambda b: b["profiles"]["final"]["distributions"]["h1_total"]["state"].update(target="total"), "target"),
])
def test_incompatible_distribution_schema_fails(trained, mutate, match):
    b = copy.deepcopy(trained[0])
    mutate(b)
    with pytest.raises(art_mod.IncompatibleArtifactError, match=match):
        art_mod.validate_bundle(b)


def test_c5d_schema1_artifact_on_disk_is_rejected(trained, tmp_path):
    b = copy.deepcopy(trained[0])
    for p in b["profiles"].values():
        p.pop("distributions")
    b["schema_version"] = 1
    d = tmp_path / "versions" / b["artifact_version"]
    d.mkdir(parents=True)
    joblib = pytest.importorskip("joblib")
    joblib.dump(b, d / art_mod.BUNDLE)
    m = art_mod.manifest_of(b, sha256=art_mod._sha256(d / art_mod.BUNDLE), gates={"passed": True})
    (d / art_mod.MANIFEST).write_text(json.dumps(m, default=str))
    (tmp_path / "CURRENT.json").write_text(json.dumps({"artifact_version": b["artifact_version"]}))
    with pytest.raises(art_mod.IncompatibleArtifactError, match="schema"):
        art_mod.load_current(tmp_path)


# ---------------- 只用樣本外殘差 ---------------- #

def test_distribution_fit_uses_only_walk_forward_oos_residuals(trained, monkeypatch):
    """被擬合的殘差 = walk-forward OOS 殘差（fold 模型只看更早的賽季），不是 production full-fit 的 in-sample 殘差。"""
    b, finals, cut, hist = trained
    seen = []
    real = dist.fit_distribution

    def spy(target, resid, **kw):
        seen.append((target, np.asarray(resid).copy()))
        return real(target, resid, **kw)

    monkeypatch.setattr(dist, "fit_distribution", spy)
    from core.production import features as pf
    frames = pf.build_training_frames(hist)
    distribution_fit.fit_all(hist, frames)
    expect = oos.walk_forward_oos(hist, first_season="2022-23", frames=frames)
    assert (expect["train_max_season"] < expect["season"]).all()
    assert "2021-22" not in set(expect["season"])                    # 第一季沒有更早的資料 → 不會有 OOS
    shared_margin = expect[expect.target == "margin"]["resid"].values
    got = [r for t, r in seen if t == "margin"]
    assert got and np.array_equal(got[0], shared_margin)
    # production full-fit 的 in-sample 殘差（同一批比賽）一定不同、且偏小
    fin = finals["final"]
    est = b["profiles"]["final"]["models"]["margin"]["estimator"]
    feats = b["profiles"]["final"]["models"]["margin"]["features"]
    ins = fin["y_margin"].values - est.predict(fin[feats].fillna(0.0))
    oos_final = expect[(expect.target == "margin") & (expect.profile == "final")]
    ins_same = ins[np.isin(fin["game_id"].values, oos_final["game_id"].values)]
    assert not np.allclose(np.sort(ins_same), np.sort(oos_final["resid"].values))
    assert np.std(ins_same) < np.std(oos_final["resid"].values)


def test_future_games_do_not_change_distributions(league):
    cut = league.games[330].game_time_utc + timedelta(hours=6)
    limit = cut - spec.TRAINING_GAME_BUFFER
    b1, _ = retrain.train_bundle(league.before(limit), cut)
    tampered = copy.deepcopy(league)
    tampered.games = [replace(g, home_pts=g.home_pts + 50) if g.game_time_utc >= limit else g for g in tampered.games]
    b2, _ = retrain.train_bundle(tampered.before(limit), cut)
    for p in spec.PROFILES:
        for t in spec.DISTRIBUTION_TARGETS:
            assert b1["profiles"][p]["distributions"][t]["state"] == b2["profiles"][p]["distributions"][t]["state"]


def test_too_few_oos_residuals_fail_retrain(league):
    short = league.before(league.games[150].game_time_utc)            # 2022-23 只有 30 場 OOS
    cut = league.games[150].game_time_utc + timedelta(hours=5)
    with pytest.raises(ValueError, match="樣本外殘差"):
        retrain.train_bundle(short, cut)


# ---------------- early / final ---------------- #

def test_profile_sharing_follows_spec(art):
    for t, cfg in spec.DISTRIBUTION_SPEC.items():
        e, f = art.profile("early")["distributions"][t], art.profile("final")["distributions"][t]
        if cfg["shared_profiles"]:
            assert e["state"] == f["state"]
        else:
            assert e["state"]["n_fit"] == f["state"]["n_fit"] and e["state"] != f["state"]
        assert e["shared_profiles"] == cfg["shared_profiles"]


def test_probability_uses_the_games_profile(art, league):
    pf_ = _game_pred(art, league, "final")
    pe = _game_pred(art, league, "early")
    a = probability.predict_h1_total_probability(pf_, 110.5, art)
    b = probability.predict_h1_total_probability(pe, 110.5, art)
    assert a["profile"] == "final" and b["profile"] == "early"
    de, df_ = art.distribution("early", "h1_total"), art.distribution("final", "h1_total")
    assert a["scale"] == pytest.approx(df_.sigma) and b["scale"] == pytest.approx(de.sigma)


# ---------------- 介面 ---------------- #

def test_line_probability_interface(art, league):
    p = _game_pred(art, league)
    pm = p.pred_margin
    res = [probability.predict_margin_probability(p, L, art) for L in (-9.5, -3, 0, 0.5, 4, 8.5)]
    for r in res:
        assert r["probability_above"] + r["probability_push"] + r["probability_below"] == pytest.approx(1.0, abs=1e-12)
        assert r["distribution_version"] == spec.DISTRIBUTION_VERSION and r["artifact_version"] == art.artifact_version
        assert r["point_prediction"] == pm and r["scale"] > 0
    assert [r["probability_above"] for r in res] == sorted([r["probability_above"] for r in res], reverse=True)
    assert res[2]["probability_push"] == 0.0 and res[1]["probability_push"] > 0      # 全場分差 0 不會平手
    t = probability.predict_total_probability(p, round(p.pred_total), art)
    assert t["probability_push"] > 0
    h = probability.predict_h1_margin_probability(p, 0, art)
    assert h["probability_push"] > 0                                                   # 上半場可平手
    # 決定性
    assert probability.predict_margin_probability(p, -3, art) == probability.predict_margin_probability(p, -3, art)
    # 錯誤輸入
    with pytest.raises(ValueError):
        probability.predict_line_probability(art, "final", "win", 0.6, 0.5)
    with pytest.raises(ValueError):
        probability.predict_line_probability(art, "final", "margin", float("nan"), 0.5)


def test_data_quality_flags_do_not_change_distribution(art):
    """C.5E：情境尺度沒有 OOS 支持 → 旗標只作 UI 警示，不改變機率。"""
    base = probability.predict_line_probability(art, "final", "total", 225.0, 230.5, flags=[])
    flagged = probability.predict_line_probability(
        art, "final", "total", 225.0, 230.5, flags=["season_opener", "injury_unknown:home", "missing_box_recent:away"],
        context=probability.distribution_context(0, 0, 0))
    for k in ("probability_above", "probability_below", "probability_push", "scale", "center"):
        assert base[k] == flagged[k]
    assert flagged["data_quality"]["affects_distribution"] is False
    assert flagged["data_quality"]["flags"][0] == "season_opener"


def test_features_json_has_distribution_summary(art, league):
    p = _game_pred(art, league)
    fj = p.features_json
    json.dumps(fj, allow_nan=False)
    s = fj["predictive_distributions"]
    assert set(s) == set(spec.DISTRIBUTION_TARGETS)
    for t, v in s.items():
        lo, hi = v["interval_80"]
        lo95, hi95 = v["interval_95"]
        assert lo95 <= lo <= v["center"] <= hi <= hi95 and v["distribution_version"] == spec.DISTRIBUTION_VERSION
    assert set(fj["distribution_context"]) == {"min_gp", "inj_both_known", "is_playoffs"}


def test_prediction_row_probability_reloads_artifact_by_version(art, league):
    p = _game_pred(art, league)
    row = {"features_json": p.features_json, "pred_margin": p.pred_margin, "pred_total": p.pred_total,
           "pred_home_h1": p.pred_home_h1, "pred_away_h1": p.pred_away_h1}
    a = probability.line_probability_from_prediction_row(row, "margin", -2.5, root=art.path.parents[1])
    b = probability.predict_margin_probability(p, -2.5, art)
    assert a == b


def test_production_location_is_frozen_zero_and_oos_mean_still_reproducible(league, trained, monkeypatch):
    """production：μ = 0（評測後修正，dist-v2）；預先登記的 oos_mean 仍可重現（對照用）。"""
    b, _, cut, hist = trained
    assert spec.DISTRIBUTION_VERSION == "dist-v2"
    assert all(c["location"] == "zero" for c in spec.DISTRIBUTION_SPEC.values())
    for p in spec.PROFILES:
        for t in spec.DISTRIBUTION_TARGETS:
            st = b["profiles"][p]["distributions"][t]["state"]
            assert st["mu"] == 0.0
    # σ = 以 0 為中心的 OOS 殘差均方根
    from core.production import features as pf
    df = distribution_fit.oos_frame(hist, pf.build_training_frames(hist))
    r = df[df.target == "margin"]["resid"].values
    assert b["profiles"]["final"]["distributions"]["margin"]["state"]["sigma"] == pytest.approx(
        np.sqrt((r ** 2).sum() / (len(r) - 1)))
    legacy = {t: {**c, "location": "oos_mean"} for t, c in spec.DISTRIBUTION_SPEC.items()}
    monkeypatch.setattr(spec, "DISTRIBUTION_SPEC", legacy)
    b2, _ = retrain.train_bundle(hist, cut)
    assert b2["profiles"]["final"]["distributions"]["margin"]["state"]["mu"] == pytest.approx(r.mean())


def test_dist_v1_artifact_is_rejected(trained):
    b = copy.deepcopy(trained[0])
    for p in b["profiles"].values():
        for d in p["distributions"].values():
            d["distribution_version"] = "dist-v1"
    with pytest.raises(art_mod.IncompatibleArtifactError, match="分佈版本"):
        art_mod.validate_bundle(b)
