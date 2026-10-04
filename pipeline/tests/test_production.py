"""C.5D production inference：artifact、重訓原子性 / 回滾、訓練-推論一致性、未來資料防護、季初 / 交易 / 缺資料"""
from __future__ import annotations

import copy
import json
import math
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest

from core.injury_asof import InjuryIndex, PlayerState
from core.models import elo, elo_state
from core.models import temporal_features as tf
from core.models import temporal_model as tm
from core.models.ml_features import build_features
from core.models.pregame_features import GameRecord, LeakageError
from core.production import artifact as art_mod
from core.production import features as pf
from core.production import inference, retrain, spec
from prod_fixtures import (MINUTES, abbr, as_scheduled, last_tip, pid_of, scheduled, snapshot, synthetic_league)


# ------------------------------------------------------------------ #
# 共用 fixture                                                         #
# ------------------------------------------------------------------ #

@pytest.fixture(scope="module")
def league():
    return synthetic_league()


@pytest.fixture(scope="module")
def trained(league):
    cut = last_tip(league) + timedelta(hours=5)
    hist = league.before(cut - spec.TRAINING_GAME_BUFFER)
    bundle, finals = retrain.train_bundle(hist, cut, trained_at=datetime(2026, 10, 3, tzinfo=pf.ensure_utc(cut).tzinfo))
    return bundle, finals, cut


def _save(bundle, finals, root, *, promote=True, version=None):
    b = dict(bundle)
    if version:
        b["artifact_version"] = version
    art_mod.save_version(b, root, gates=retrain.run_gates(b, finals), verify=retrain.roundtrip_verifier(b, finals))
    if promote:
        art_mod.promote(b["artifact_version"], root)
    return b["artifact_version"]


@pytest.fixture()
def art(trained, tmp_path):
    bundle, finals, _ = trained
    _save(bundle, finals, tmp_path)
    return art_mod.load_current(tmp_path)


def _blend(prof):
    return {tuple(k.split("|")): tm.BlendParams(**v) for k, v in prof["blend_params"].items()}


def _prod_inputs(art, history, games, as_of, profile, pending=None):
    ctx = pf.prepare_context(history.before(as_of), as_of)
    frame = pf.upcoming_rows(ctx, games, as_of, art.calibrator(profile), pending or [])
    prof = art.profile(profile)
    return pf.finalize(frame, _blend(prof), prof["fills"]), prof


# ------------------------------------------------------------------ #
# 1. artifact save / load parity                                     #
# ------------------------------------------------------------------ #

def test_artifact_save_load_parity(trained, tmp_path):
    bundle, finals, cut = trained
    v = _save(bundle, finals, tmp_path)
    loaded = art_mod.load_current(tmp_path)
    assert loaded.artifact_version == v and art_mod.current_version(tmp_path) == v
    for name, fin in finals.items():
        a = inference.predict_profile(bundle["profiles"][name], fin)
        b = inference.predict_profile(loaded.profile(name), fin)
        for t in a:
            assert np.array_equal(a[t], b[t]), (name, t)
        # 特徵清單 / 順序、補值常數、收縮參數、校準狀態完全保存
        for t in spec.PRODUCTION_SPEC:
            assert loaded.profile(name)["models"][t]["features"] == bundle["profiles"][name]["models"][t]["features"]
        assert loaded.profile(name)["fills"] == bundle["profiles"][name]["fills"]
        assert loaded.profile(name)["blend_params"] == bundle["profiles"][name]["blend_params"]
        cal = loaded.calibrator(name)
        assert cal.frozen
        orig = tf.InjuryCalibrator.from_state(bundle["profiles"][name]["calibrator"], frozen=False)
        for s in tf.STATUS_ORDER:
            for lead in (1.0, 10.0):
                assert cal.p_absent(s, lead) == orig.p_absent(s, lead)
    m = json.loads((Path(loaded.path) / art_mod.MANIFEST).read_text())
    for k in ("model_version", "artifact_version", "feature_version", "training_cutoff_utc", "training_seasons",
              "schema_version", "library_versions", "sha256", "gates"):
        assert k in m
    assert m["training_cutoff_utc"] == cut.isoformat() and m["known_good"] is True
    assert bundle["metadata"]["n_train"]["win"] > 0


def test_frozen_calibrator_cannot_learn(art):
    cal = art.calibrator("final")
    with pytest.raises(RuntimeError, match="凍結"):
        cal.observe("Out", 1.0, True, pf.ensure_utc(datetime(2024, 1, 1)))


def test_inference_cannot_recompute_fills(art, league):
    prof = art.profile("final")
    fills = dict(prof["fills"])
    fills.pop(next(iter(fills)))
    tip = last_tip(league) + timedelta(days=1)
    ctx = pf.prepare_context(league, tip - timedelta(hours=1))
    frame = pf.upcoming_rows(ctx, [scheduled(9001, tip, 1, 2)], tip - timedelta(hours=1), art.calibrator("final"))
    with pytest.raises(pf.FillsError):
        pf.finalize(frame, _blend(prof), fills)


# ------------------------------------------------------------------ #
# 2. 版本不相容必須明確失敗                                              #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("mutate,match", [
    (lambda b: b.update(schema_version=99), "schema_version"),
    (lambda b: b.update(feature_version="pregame-v2"), "特徵版本"),
    (lambda b: b.update(model_version="ml-v9"), "model_version"),
    (lambda b: b.update(library_versions={**b["library_versions"], "sklearn": "0.24.2"}), "scikit-learn"),
    (lambda b: b["profiles"].pop("early"), "profile early"),
    (lambda b: b["profiles"]["final"]["models"].pop("total"), "total"),
    (lambda b: b["profiles"]["final"]["calibrator"].update(m_status=99.0), "傷病校準"),
    (lambda b: b.pop("training_cutoff_utc"), "缺少欄位"),
])
def test_incompatible_bundle_fails_clearly(trained, mutate, match):
    bundle = copy.deepcopy(trained[0])
    mutate(bundle)
    with pytest.raises(art_mod.IncompatibleArtifactError, match=match):
        art_mod.validate_bundle(bundle)


def test_incompatible_files_fail_on_load(trained, tmp_path):
    bundle, finals, _ = trained
    v = _save(bundle, finals, tmp_path)
    d = tmp_path / "versions" / v
    # 1) manifest 記錄的 sklearn 版本不同 → 在 unpickle 之前就拒絕
    m = json.loads((d / art_mod.MANIFEST).read_text())
    m2 = {**m, "library_versions": {**m["library_versions"], "sklearn": "1.0.0"}}
    (d / art_mod.MANIFEST).write_text(json.dumps(m2))
    with pytest.raises(art_mod.IncompatibleArtifactError, match="scikit-learn"):
        art_mod.load_current(tmp_path)
    (d / art_mod.MANIFEST).write_text(json.dumps(m))
    # 2) bundle 被改動 → 雜湊不符
    with open(d / art_mod.BUNDLE, "ab") as f:
        f.write(b"tamper")
    with pytest.raises(art_mod.IncompatibleArtifactError, match="雜湊"):
        art_mod.load_current(tmp_path)


def test_missing_artifact_fails_clearly(tmp_path):
    with pytest.raises(art_mod.NoProductionArtifact, match="run_retrain"):
        art_mod.load_current(tmp_path)


# ------------------------------------------------------------------ #
# 3. 重訓原子性 / 決定性 / 回滾                                           #
# ------------------------------------------------------------------ #

def test_retrain_is_deterministic(league):
    cut = last_tip(league) + timedelta(hours=5)
    hist = league.before(cut - spec.TRAINING_GAME_BUFFER)
    b1, f1 = retrain.train_bundle(hist, cut)
    b2, f2 = retrain.train_bundle(hist, cut)
    for name in spec.PROFILES:
        p1 = inference.predict_profile(b1["profiles"][name], f1[name])
        p2 = inference.predict_profile(b2["profiles"][name], f1[name])
        for t in p1:
            assert np.array_equal(p1[t], p2[t])
        assert b1["profiles"][name]["fills"] == b2["profiles"][name]["fills"]
        assert b1["profiles"][name]["calibrator"] == b2["profiles"][name]["calibrator"]
    assert b1["metadata"]["data_fingerprint"] == b2["metadata"]["data_fingerprint"]


def test_retrain_only_uses_games_before_cutoff_buffer(league, tmp_path):
    cut = league.games[300].game_time_utc + timedelta(hours=2)        # 第 301 場開賽後 2 小時：未滿 buffer
    res = retrain.retrain(cut, root=tmp_path, history_loader=lambda limit: league)
    assert res.n_games == 300
    b = art_mod.load_current(tmp_path).bundle
    assert pf.ensure_utc(datetime.fromisoformat(b["metadata"]["last_game_utc"])) < cut - spec.TRAINING_GAME_BUFFER
    with pytest.raises(ValueError, match="cutoff"):
        retrain.train_bundle(league, cut)                                 # 未截斷的資料直接拒絕


def test_retrain_failure_keeps_current_and_leaves_no_partial_version(league, tmp_path):
    cut1 = league.games[250].game_time_utc
    r1 = retrain.retrain(cut1, root=tmp_path, history_loader=lambda limit: league,
                         trained_at=datetime(2026, 1, 1, tzinfo=cut1.tzinfo))
    before = sorted(p.name for p in (tmp_path / "versions").iterdir())
    cut2 = last_tip(league) + timedelta(hours=5)
    # (a) 寫完 bundle 後失敗
    with pytest.raises(art_mod.ArtifactError, match="模擬"):
        retrain.retrain(cut2, root=tmp_path, history_loader=lambda limit: league, _fail_after_bundle=True)
    # (b) 訓練途中失敗（資料載入 / 擬合例外）
    def boom(limit):
        raise RuntimeError("DB down")
    with pytest.raises(RuntimeError, match="DB down"):
        retrain.retrain(cut2, root=tmp_path, history_loader=boom)
    assert art_mod.current_version(tmp_path) == r1.artifact_version
    assert sorted(p.name for p in (tmp_path / "versions").iterdir()) == before          # 無 .tmp-*、無半成品
    assert art_mod.load_current(tmp_path).artifact_version == r1.artifact_version       # 仍可正常載入


def test_retrain_gate_failure_does_not_write(league, tmp_path, monkeypatch):
    monkeypatch.setattr(retrain, "run_gates", lambda b, f: {"passed": False, "checks": {"x": False}})
    with pytest.raises(retrain.RetrainGateFailed):
        retrain.retrain(last_tip(league) + timedelta(hours=5), root=tmp_path, history_loader=lambda limit: league)
    assert art_mod.current_version(tmp_path) is None
    assert not [p for p in (tmp_path / "versions").glob("*")]


def test_roundtrip_mismatch_aborts_before_rename(trained, tmp_path):
    bundle, finals, _ = trained
    def bad_verify(loaded):
        raise retrain.RetrainGateFailed("預測不一致")
    with pytest.raises(retrain.RetrainGateFailed):
        art_mod.save_version(bundle, tmp_path, gates={"passed": True}, verify=bad_verify)
    assert not list((tmp_path / "versions").iterdir())


def test_concurrent_retrain_is_rejected(league, tmp_path):
    with art_mod.exclusive_lock(tmp_path):
        with pytest.raises(art_mod.LockBusy):
            retrain.retrain(last_tip(league) + timedelta(hours=5), root=tmp_path, history_loader=lambda limit: league)


def test_interrupted_tmp_dir_is_ignored_and_cleaned(trained, league, tmp_path):
    bundle, finals, _ = trained
    v = _save(bundle, finals, tmp_path)
    (tmp_path / "versions" / ".tmp-crashed").mkdir()
    assert [x["artifact_version"] for x in art_mod.list_versions(tmp_path)] == [v]
    retrain.retrain(last_tip(league) + timedelta(hours=6), root=tmp_path, history_loader=lambda limit: league)
    assert not (tmp_path / "versions" / ".tmp-crashed").exists()


def test_rollback_and_previous_versions_are_kept(trained, tmp_path):
    bundle, finals, _ = trained
    v1 = _save(bundle, finals, tmp_path, version="ml-v2.0+A")
    v2 = _save(bundle, finals, tmp_path, version="ml-v2.0+B")
    assert art_mod.current_version(tmp_path) == v2
    p = art_mod.rollback(tmp_path)
    assert p["artifact_version"] == v1 and p["previous"] == v2
    assert art_mod.load_current(tmp_path).artifact_version == v1
    assert {x["artifact_version"] for x in art_mod.list_versions(tmp_path)} == {v1, v2}     # 不刪除任何版本
    art_mod.rollback(tmp_path, to=v2)
    assert art_mod.current_version(tmp_path) == v2
    with pytest.raises(art_mod.ArtifactError):
        art_mod.rollback(tmp_path, to="ml-v2.0+missing")
    events = [e["event"] for e in art_mod.read_history(tmp_path)]
    assert events == ["promote", "promote", "rollback", "rollback"]


def test_promote_rejects_not_known_good(trained, tmp_path):
    bundle, finals, _ = trained
    art_mod.save_version(bundle, tmp_path, gates={"passed": False})
    with pytest.raises(art_mod.ArtifactError, match="未通過"):
        art_mod.promote(bundle["artifact_version"], tmp_path)
    assert art_mod.current_version(tmp_path) is None


def test_version_dirs_are_immutable(trained, tmp_path):
    bundle, finals, _ = trained
    _save(bundle, finals, tmp_path)
    with pytest.raises(art_mod.ArtifactError, match="已存在"):
        art_mod.save_version(bundle, tmp_path, gates={"passed": True})


# ------------------------------------------------------------------ #
# 4. 訓練 vs production 特徵一致性                                        #
# ------------------------------------------------------------------ #

def test_elo_replay_matches_backtest_rules(league):
    """與 jobs/backtest.py 相同的規則：新賽季迴歸、主場加成、margin-of-victory。"""
    ratings, last_season, ref = {}, {}, {}
    for g in sorted(league.games, key=lambda g: (g.game_time_utc, g.game_id)):
        for tid in (g.home_team_id, g.away_team_id):
            ratings.setdefault(tid, elo.INITIAL_RATING)
            if last_season.get(tid) and last_season[tid] != g.season:
                ratings[tid] = elo.regress_to_mean(ratings[tid])
            last_season[tid] = g.season
        rh, ra = ratings[g.home_team_id], ratings[g.away_team_id]
        ref[g.game_id] = (rh, ra)
        nh, na = elo.update_ratings(rh, ra, home_won=g.home_pts > g.away_pts, margin=g.home_pts - g.away_pts,
                                    rating_diff=rh + elo.HOME_ADVANTAGE - ra)
        ratings[g.home_team_id], ratings[g.away_team_id] = nh, na
    st, before = elo_state.replay(league.games)
    assert before == ref
    # 未開賽的新賽季比賽：回傳迴歸後的值，不改動狀態
    rh, ra = st.pregame(1, 2, "2024-25")
    assert rh == elo.regress_to_mean(st.ratings[1]) and st.last_season[1] == "2023-24"


def test_phase_c_state_matches_pandas_build_features(league):
    """PhaseCState（推論）與 ml_features.build_features（訓练）的 shift(1)+rolling 語意一致。"""
    _, before = elo_state.replay(league.games)
    hist = pf.phase_c_frame(league, before).set_index("game_id")
    st = pf.PhaseCState()
    cols = ["home_rest_days", "away_rest_days", "home_b2b", "away_b2b", "home_games_7d", "away_games_7d",
            "home_form10", "away_form10", "home_margin10", "away_margin10", "total_est", "h1_total_est",
            "is_playoffs", "rest_diff", "b2b_diff", "form10_diff", "margin10_diff"]
    n = 0
    for g in sorted(league.games, key=lambda g: (g.game_time_utc, g.game_id)):
        stage = league.stages[g.game_id]
        if stage in pf.PHASE_C_STAGES:
            row = pf.phase_c_row(st, g, stage, g.game_time_utc)
            for c in cols:
                a, b = row[c], hist.loc[g.game_id, c]
                assert (pd.isna(a) and pd.isna(b)) or a == pytest.approx(b, abs=1e-9), (g.game_id, c, a, b)
            n += 1
        st.absorb(g, stage)
    assert n > 300


def _training_row_inputs(bundle, finals, profile, gid):
    fin = finals[profile]
    row = fin[fin["game_id"] == gid]
    assert len(row) == 1
    return {t: inference.model_inputs(bundle["profiles"][profile], row, t).iloc[0] for t in spec.PRODUCTION_SPEC}


@pytest.mark.parametrize("profile,offset", [("final", timedelta(minutes=60)), ("early", None)])
def test_training_serving_parity_on_historical_games(league, trained, profile, offset):
    """對歷史比賽 G：把 G 當成未開賽（不給比分），以 production 路徑在 G 的決策時點建特徵 → 所有模型輸入
    必須與訓練表中 G 那一列完全相同（傷病校準器取「訓練時 G 當下」的 walk-forward 狀態）。"""
    bundle, finals, cut = trained
    games = sorted(league.games, key=lambda g: (g.game_time_utc, g.game_id))
    picks = [games[5], games[121], games[125], games[200], games[300], games[-20]]   # 含新賽季第 1~5 場、季中
    if profile == "early":
        # NBA 球隊不會在同一個 ET 賽事日打兩場；合成賽程每 12 小時一場會違反這點，所以 early 只挑
        # 「兩隊在 ET 00:00 ~ 開賽之間沒有其他比賽」的場次（否則訓練狀態本身就含 cutoff 之後才結束的比賽）
        def clean(g):
            lo = tf.make_cutoff("early")(g.game_time_utc)
            return not any(lo <= o.game_time_utc < g.game_time_utc and {o.home_team_id, o.away_team_id}
                           & {g.home_team_id, g.away_team_id} for o in games)
        picks = [g for g in games[5::17] if clean(g)][:6]
        assert len(picks) >= 4
    for g in picks:
        tip = g.game_time_utc
        as_of = tip - offset if offset else tf.make_cutoff("early")(tip)
        hist = league.before(tip)
        _, cal = tf.build_temporal_features(hist.games, hist.derived, hist.players, hist.injury_index,
                                            timing=spec.PROFILES[profile], return_calibrator=True)
        cal = tf.InjuryCalibrator.from_state(cal.to_state(), frozen=True)
        ctx = pf.prepare_context(hist.before(as_of), as_of)
        frame = pf.upcoming_rows(ctx, [as_scheduled(g, league.stages[g.game_id])], as_of, cal)
        prof = bundle["profiles"][profile]
        fin = pf.finalize(frame, _blend(prof), prof["fills"])
        wants = _training_row_inputs(bundle, finals, profile, g.game_id)
        for t in spec.PRODUCTION_SPEC:
            got, want = inference.model_inputs(prof, fin, t).iloc[0], wants[t]
            bad = [(c, got[c], want[c]) for c in got.index if abs(got[c] - want[c]) > 1e-9]
            assert not bad, (g.game_id, profile, t, bad)


C5C_CACHE = Path(__file__).resolve().parents[1] / "artifacts" / "c5c_inputs.pkl"


@pytest.mark.skipif(not C5C_CACHE.exists(), reason="需要 c5c_inputs.pkl（真實歷史資料快照）")
def test_training_serving_parity_on_real_history():
    """真實五季資料（含交易、缺 box、傷病未涵蓋）：以固定傷病常數排除校準器差異，比較 production 路徑與訓練路徑的模型輸入。"""
    from core.jobs import c5c_inputs
    inp = c5c_inputs.load()
    st = dict(zip(inp.phase_c["game_id"], inp.phase_c["season_stage"]))
    hist = pf.HistoryInputs(inp.games, {g.game_id: st.get(g.game_id, "playin") for g in inp.games},
                            inp.derived, inp.players, inp.injury_index)
    fixed = {"Out": 1.0, "Doubtful": 0.75, "Questionable": 0.5, "Probable": 0.15, "Available": 0.0, "Not Listed": 0.0}
    v2 = tf.build_temporal_features(hist.games, hist.derived, hist.players, hist.injury_index, timing="T-60",
                                    fixed_p_absent=fixed)
    _, before = elo_state.replay(hist.games)
    frame = pf.side_targets(pf.model_frame(v2, pf.phase_c_frame(hist, before), pf.elo_frame(before)),
                            {g.game_id: g for g in hist.games}, hist.derived)
    params = tm.fit_all_blends(frame)
    fin_train, fills = tm.add_model_columns(tm.apply_blends(frame, params))
    feats = sorted({f for t, c in spec.PRODUCTION_SPEC.items() for f in tm.TARGETS[t][1][c["group"]]})
    cal = tf.InjuryCalibrator(fixed=fixed, frozen=True)
    rng = np.random.default_rng(0)
    ids = fin_train[fin_train["season"] == "2025-26"]["game_id"].values
    pick = list(rng.choice(ids, 12, replace=False)) + [int(fin_train[fin_train["season"] == "2025-26"]["game_id"].iloc[0])]
    games = {g.game_id: g for g in hist.games}
    for gid in pick:
        g = games[int(gid)]
        as_of = g.game_time_utc - timedelta(minutes=60)
        ctx = pf.prepare_context(hist.before(as_of), as_of)
        fr = pf.upcoming_rows(ctx, [as_scheduled(g, hist.stages[g.game_id])], as_of, cal)
        got = pf.finalize(fr, params, fills)[feats].iloc[0].astype(float).fillna(0.0)
        want = fin_train[fin_train["game_id"] == g.game_id][feats].iloc[0].astype(float).fillna(0.0)
        bad = [(c, got[c], want[c]) for c in feats if abs(got[c] - want[c]) > 1e-9]
        assert not bad, (g.game_id, bad)


def test_schedule_features_count_pending_prior_game(league, art):
    """背靠背的第一場尚未打完：休息/背靠背照賽程算（與訓練語意一致），近況停在最後一場已完賽比賽。"""
    last = last_tip(league)
    t1 = last + timedelta(days=2)
    pending = scheduled(8001, t1, 1, 3)
    target = scheduled(8002, t1 + timedelta(hours=24), 1, 2)
    as_of = t1 - timedelta(hours=2)
    fin, _ = _prod_inputs(art, league, [target], as_of, "early", [pending])
    r = fin.iloc[0]
    assert r["home_rest_days_v2"] == 0.0 and r["home_b2b_v2"] == 1.0           # 由排定的前一場算
    assert r["home_rest_days"] == 0.0 and r["home_b2b"] == 1.0                 # Phase C 版
    fin2, _ = _prod_inputs(art, league, [target], as_of, "early", [])
    assert fin2.iloc[0]["home_rest_days_v2"] > 0.5                             # 沒有 pending → 從最後完賽算
    assert r["home_gp"] == fin2.iloc[0]["home_gp"]                             # 結果未知：本季場數不變
    preds, _ = inference.predict_games(art, league, [target], [pending], as_of, kind="early")
    assert "pending_prior_game:home" in preds[0].quality.flags


# ------------------------------------------------------------------ #
# 5. 未來資料防護                                                        #
# ------------------------------------------------------------------ #

def test_future_leakage_guard(league, art):
    """預測時間戳之後的任何資料（之後比賽的結果、這場自己的結果、之後的傷病報告）都不能影響特徵。"""
    games = sorted(league.games, key=lambda g: (g.game_time_utc, g.game_id))
    g = games[300]
    as_of = g.game_time_utc - timedelta(minutes=60)
    sg = as_scheduled(g)
    base, prof = _prod_inputs(art, league, [sg], as_of, "final")
    # 竄改：之後所有比賽（含這場）的比分 / box / 球員分鐘，並加一份 as_of 之後的報告把主隊主力列 Out
    tampered = copy.deepcopy(league)
    tampered.games = [replace(x, home_pts=x.home_pts + 40, away_pts=0) if x.game_time_utc >= g.game_time_utc else x
                      for x in tampered.games]
    for x in tampered.games:
        if x.game_time_utc >= g.game_time_utc:
            tampered.derived[(x.game_id, x.home_team_id)] = {k: 999.0 for k in tampered.derived[(x.game_id, x.home_team_id)]}
            tampered.players[x.game_id] = []
    late = snapshot(10 ** 6, as_of + timedelta(minutes=1), g.game_time_utc, [g.home_team_id],
                    {g.home_team_id: [(pid_of(g.home_team_id, 0, 2), "Out")]})
    tampered.injury_index = InjuryIndex(list(tampered.injury_index.snapshots) + [late])
    got, _ = _prod_inputs(art, tampered, [sg], as_of, "final")
    for t in spec.PRODUCTION_SPEC:
        pd.testing.assert_frame_equal(inference.model_inputs(prof, base, t), inference.model_inputs(prof, got, t))
    # 歷史輸入含預測時間戳之後的比賽 → 直接拒絕
    with pytest.raises(LeakageError):
        pf.prepare_context(league, as_of)
    # 已開賽 / 預測時間戳不早於開賽 → 拒絕
    ctx = pf.prepare_context(league.before(as_of), as_of)
    with pytest.raises(LeakageError):
        pf.upcoming_rows(ctx, [replace(sg, game_time_utc=as_of)], as_of, art.calibrator("final"))
    # 推論不可使用未凍結（會學習）的校準器
    with pytest.raises(ValueError, match="凍結"):
        pf.upcoming_rows(ctx, [sg], as_of, tf.InjuryCalibrator())


def test_predict_games_skips_started_or_imminent_games(league, art):
    as_of = last_tip(league) + timedelta(hours=10)
    preds, skipped = inference.predict_games(art, league, [scheduled(1, as_of + timedelta(minutes=3), 1, 2)], [],
                                             as_of, kind="final")
    assert not preds and skipped and "5 分鐘" in skipped[0].reason


# ------------------------------------------------------------------ #
# 6. 季初 / 開季第一場                                                    #
# ------------------------------------------------------------------ #

def test_season_opener_and_early_season_fallback(league, art):
    """新賽季第一場：本季樣本 0、名單延續性未知 → 不當成 0；先驗收縮用訓練集 c̄；標示 season_opener / low_sample。"""
    tip = last_tip(league) + timedelta(days=120)
    sg = scheduled(7001, tip, 1, 2, season="2024-25")
    as_of = tip - timedelta(hours=20)
    fin, prof = _prod_inputs(art, league, [sg], as_of, "early")
    r = fin.iloc[0]
    assert r["home_gp"] == 0 and r["away_gp"] == 0
    assert math.isnan(r["home_ret_min_pct"]) and math.isnan(r["home_roster_prior_pm48"])      # 未知 = NaN，不是 0
    assert r["cont_known"] == 0.0 and r["early_season"] == 1.0
    assert r["ret_min_pct_diff"] == 0.0                                                     # 差值中性（不偏任何一方）
    bp = _blend(prof)[("est_net_rtg", "blend")]
    L, P = r["league_est_net_rtg_prev"], r["home_est_net_rtg_prev"]
    assert not math.isnan(P)
    assert r["home_est_net_rtg_blend"] == pytest.approx(L + (bp.a + bp.b * bp.c_bar) * (P - L))   # c̄（≈訓練集季初平均）
    assert 0.3 < bp.c_bar < 1.0
    assert r["ret_min_pct_sum"] == pytest.approx(prof["fills"]["ret_min_pct"] * 2)          # 訓練集平均，不是 0
    preds, _ = inference.predict_games(art, league, [sg], [], as_of, kind="early")
    p = preds[0]
    fj = p.features_json
    assert {"season_opener", "low_sample", "early_season", "continuity_unknown:home"} <= set(p.quality.flags)
    assert fj["early_season"] is True and fj["low_sample"] is True and fj["continuity_known"] is False
    assert p.confidence == pytest.approx(round(fj["confidence_raw"] * p.quality.factor, 4))
    assert p.quality.factor == pytest.approx(0.6 * 0.8)        # 樣本組取最嚴重（開季）× 傷病未知；不重複懲罰
    assert all(math.isfinite(v) for v in p.row_values().values())


def test_games_one_to_five_are_low_sample(league, art):
    new = synthetic_league(seed=5, games_per_season=8, seasons=["2024-25"], injuries=False)
    off = 2000
    hist = copy.deepcopy(league)
    shift = last_tip(league) + timedelta(days=120) - new.games[0].game_time_utc
    for g in new.games:
        ng = replace(g, game_id=g.game_id + off, game_time_utc=g.game_time_utc + shift)
        hist.games.append(ng)
        hist.stages[ng.game_id] = "regular"
        hist.derived.update({(ng.game_id, k[1]): v for k, v in new.derived.items() if k[0] == g.game_id})
        hist.players[ng.game_id] = new.players[g.game_id]
    tip = last_tip(hist) + timedelta(days=1)
    as_of = tip - timedelta(hours=1)
    preds, _ = inference.predict_games(art_mod.load_current(art.path.parents[1]), hist,
                                       [scheduled(7002, tip, 1, 2, season="2024-25")], [], as_of, kind="final")
    p = preds[0]
    gp = p.features_json["games_played_home"], p.features_json["games_played_away"]
    assert 1 <= min(gp) < 5
    assert "low_sample" in p.quality.flags and "season_opener" not in p.quality.flags
    assert p.features_json["continuity_known"] is True


# ------------------------------------------------------------------ #
# 7. 交易球員：重要性跟著球員走、可用性屬於新隊                                 #
# ------------------------------------------------------------------ #

def _trade_league():
    """X 是先發（取代 k=4 的先發位置）：前 26 場替 A(1) 出賽，之後改替 B(2) 出賽（交易）。
    A 最近 10 場中 7 場有 X → 舊定義會把 X 繼續算成 A 的預期先發。"""
    from core.timeutil import UTC
    t0 = datetime(2023, 10, 24, 23, 0, tzinfo=UTC)
    X = 1000
    games, derived, players, stages = [], {}, {}, {}
    pairs = [(1, 3), (2, 4), (3, 2), (4, 1)]
    gid = 0
    for i in range(32):
        h, a = pairs[i % 4]
        gid += 1
        tip = t0 + timedelta(hours=20 * i)
        games.append(GameRecord(gid, "2023-24", tip, h, a, abbr(h), abbr(a), 110 + i % 7, 104 + i % 5, 55, 52))
        stages[gid] = "regular"
        for t in (h, a):
            derived[(gid, t)] = {"pace": 99.0, "ortg": 112.0 + t, "drtg": 110.0, "net_rtg": 2.0 + t, "efg_pct": 0.54,
                                 "ts_pct": 0.58, "tov_pct": 0.13, "orb_pct": 0.25, "drb_pct": 0.75, "ftr": 0.2,
                                 "fg3a_rate": 0.4}
        lines = []
        for t in (h, a):
            for k, m in enumerate(MINUTES):
                lines.append((t, t * 100 + k, m, k < 5, 0.0))
        traded = i >= 26
        for t in (h, a):
            if (t == 1 and not traded) or (t == 2 and traded):
                lines = [ln for ln in lines if not (ln[0] == t and ln[1] == t * 100 + 4)]
                lines.append((t, X, 38.0, True, 5.0))
        players[gid] = lines
    return pf.HistoryInputs(games, stages, derived, players, InjuryIndex([])), X


def test_traded_player_maps_to_new_team():
    hist, X = _trade_league()
    tip = max(g.game_time_utc for g in hist.games) + timedelta(days=1)
    as_of = tip - timedelta(hours=1)
    # 兩隊都有涵蓋報告：B 把 X 列 Out；A 的報告當然不會列他
    snap = snapshot(1, as_of - timedelta(minutes=30), tip, [1, 2], {1: [], 2: [(X, "Out")]})
    hist.injury_index = InjuryIndex([snap])
    games = [scheduled(500, tip, 1, 2)]
    cal = tf.InjuryCalibrator(fixed={"Out": 1.0, "Not Listed": 0.0}, frozen=True)
    ctx = pf.prepare_context(hist, as_of)
    assert ctx.builder.player_team[X] == 2
    r = pf.upcoming_rows(ctx, games, as_of, cal).iloc[0]
    # A（主隊）：X 已不屬於 A → 不在 A 的預期先發 / 輪替；A 的離隊人數 = 1
    assert r["home_roster_departed_n"] == 1
    assert r["home_inj_exp_starters_avail"] == 5.0 and r["home_inj_rotation_avail_pct"] == 1.0
    # B（客隊）：X 被列 Out → 重要性來自他過去的上場分鐘（含替 A 的時期），可用性屬於 B
    role = np.mean([38.0] * 10)
    assert r["away_inj_min_lost_role"] == pytest.approx(role)
    assert r["away_inj_n_out"] == 1
    # 對照：舊定義（trade_aware=False）會把 X 繼續算進 A 的預期先發（不該發生）
    ctx_old = pf.prepare_context(hist, as_of, trade_aware=False)
    r_old = pf.upcoming_rows(ctx_old, games, as_of, cal).iloc[0]
    assert r_old["home_roster_departed_n"] == 0
    exp_old = tf._expected_starters(ctx_old.builder.teams[1])
    exp_new = tf._expected_starters(ctx.builder.teams[1], ctx.builder.on_team(1))
    assert X in exp_old and X not in exp_new


def test_traded_player_on_old_team_report_before_trade():
    """交易前（X 最近一場仍替 A 出賽）：A 的報告列他 Out → 計入 A 的損失。"""
    hist, X = _trade_league()
    pre = [g for g in hist.games if g.game_id <= 26]
    h = pf.HistoryInputs(pre, {g.game_id: "regular" for g in pre},
                         {k: v for k, v in hist.derived.items() if k[0] <= 26},
                         {k: v for k, v in hist.players.items() if k <= 26}, InjuryIndex([]))
    tip = max(g.game_time_utc for g in pre) + timedelta(days=1)
    as_of = tip - timedelta(hours=1)
    h.injury_index = InjuryIndex([snapshot(1, as_of - timedelta(minutes=30), tip, [1, 2], {1: [(X, "Out")], 2: []})])
    cal = tf.InjuryCalibrator(fixed={"Out": 1.0, "Not Listed": 0.0}, frozen=True)
    r = pf.upcoming_rows(pf.prepare_context(h, as_of), [scheduled(501, tip, 1, 2)], as_of, cal).iloc[0]
    assert r["home_inj_min_lost_role"] == pytest.approx(38.0) and r["home_inj_exp_starters_avail"] == 4.0


# ------------------------------------------------------------------ #
# 8. 缺資料 / 來源降級                                                    #
# ------------------------------------------------------------------ #

def test_missing_injury_report_fallback(league, art):
    """沒有任何報告涵蓋 → inj_known=0、傷病差值中性（非 0 傷亡的假設：只是不偏向任何一方）、旗標 + 降低信心，仍可預測。"""
    tip = last_tip(league) + timedelta(days=1)
    as_of = tip - timedelta(hours=1)
    no_inj = copy.deepcopy(league)
    no_inj.injury_index = InjuryIndex([])
    preds, _ = inference.predict_games(art, no_inj, [scheduled(6001, tip, 1, 2)], [], as_of, kind="final")
    p = preds[0]
    assert {"injury_unknown:home", "injury_unknown:away", "injury_feed_stale"} <= set(p.quality.flags)
    assert p.features_json["injuries"]["home"]["known"] is False
    assert p.features_json["injuries"]["home"]["n_out"] is None                       # 未知，不寫成 0
    assert p.quality.factor < 1.0 and all(math.isfinite(v) for v in p.row_values().values())
    fin, prof = _prod_inputs(art, no_inj, [scheduled(6001, tip, 1, 2)], as_of, "final")
    assert fin.iloc[0]["inj_both_known"] == 0 and fin.iloc[0]["inj_min_lost_role_diff"] == 0


def test_injury_report_changes_prediction(league, art):
    tip = last_tip(league) + timedelta(days=1)
    as_of = tip - timedelta(hours=1)
    sg = scheduled(6002, tip, 1, 2)
    base = copy.deepcopy(league)
    base.injury_index = InjuryIndex(list(base.injury_index.snapshots)
                                    + [snapshot(10 ** 6, as_of - timedelta(minutes=40), tip, [1, 2], {1: [], 2: []})])
    out = copy.deepcopy(league)
    out.injury_index = InjuryIndex(list(out.injury_index.snapshots) + [
        snapshot(10 ** 6, as_of - timedelta(minutes=40), tip, [1, 2], {1: [(pid_of(1, 0, 2), "Out")], 2: []})])
    p0 = inference.predict_games(art, base, [sg], [], as_of, kind="final")[0][0]
    p1 = inference.predict_games(art, out, [sg], [], as_of, kind="final")[0][0]
    assert p1.home_win_prob < p0.home_win_prob and p1.input_hash != p0.input_hash
    assert p1.features_json["injuries"]["home"]["n_out"] == 1
    again = inference.predict_games(art, base, [sg], [], as_of, kind="final")[0][0]
    assert again.input_hash == p0.input_hash and again.home_win_prob == p0.home_win_prob      # 決定性


def test_partial_source_failure_flags_and_no_fabrication(league, art):
    """CDN 部分失敗：近幾場缺 box / 衍生指標 → 只用現有資料（不補造），標示 missing_box_recent；
    應已結束卻未同步的比賽 → stale_results；預測仍允許但信心度降低。"""
    hist = copy.deepcopy(league)
    team1 = [g for g in sorted(hist.games, key=lambda g: g.game_time_utc) if 1 in (g.home_team_id, g.away_team_id)]
    for g in team1[-3:]:
        hist.derived.pop((g.game_id, 1), None)
        hist.players[g.game_id] = [ln for ln in hist.players[g.game_id] if ln[0] != 1]
    tip = last_tip(hist) + timedelta(days=2)
    as_of = tip - timedelta(hours=1)
    stale = scheduled(6100, as_of - timedelta(hours=20), 1, 4)                  # 開賽 20 小時前、仍非 final
    preds, _ = inference.predict_games(art, hist, [scheduled(6003, tip, 1, 2)], [stale], as_of, kind="final")
    p = preds[0]
    assert {"missing_box_recent:home", "pending_prior_game:home", "stale_results:home"} <= set(p.quality.flags)
    assert p.quality.details["missing_box_recent_home"] == 3
    assert all(math.isfinite(v) for v in p.row_values().values())
    # 不補造：缺 box 的比賽不進入 est_* 平均（與只用剩餘比賽手算一致）
    fin, _ = _prod_inputs(art, hist, [scheduled(6003, tip, 1, 2)], as_of, "final", [stale])
    season = [g for g in team1 if g.season == "2023-24" and (g.game_id, 1) in hist.derived]
    want = np.mean([hist.derived[(g.game_id, 1)]["ortg"] for g in season])
    assert fin.iloc[0]["home_est_off_rtg_season"] == pytest.approx(want)


def test_new_player_without_history_is_flagged(league, art):
    tip = last_tip(league) + timedelta(days=1)
    as_of = tip - timedelta(hours=1)
    h = copy.deepcopy(league)
    h.injury_index = InjuryIndex([snapshot(10 ** 6, as_of - timedelta(minutes=30), tip, [1, 2],
                                           {1: [(424242, "Out")], 2: [(None, "Out")]})])
    p = inference.predict_games(art, h, [scheduled(6004, tip, 1, 2)], [], as_of, kind="final")[0][0]
    assert "injury_player_no_history:home" in p.quality.flags            # 沒有出賽紀錄 → 不計入分鐘損失
    assert "injury_unmatched_players:away" in p.quality.flags            # 姓名對不到球員
    fin, _ = _prod_inputs(art, h, [scheduled(6004, tip, 1, 2)], as_of, "final")
    assert fin.iloc[0]["home_inj_min_lost_role"] == 0.0 and fin.iloc[0]["home_inj_n_out"] == 1


def test_playin_game_is_predicted_but_flagged(league, art):
    tip = last_tip(league) + timedelta(days=1)
    p = inference.predict_games(art, league, [scheduled(6005, tip, 1, 2, stage="playin")], [],
                                tip - timedelta(hours=1), kind="final")[0][0]
    assert "stage_out_of_training" in p.quality.flags


# ------------------------------------------------------------------ #
# 9. 解釋（features_json）                                               #
# ------------------------------------------------------------------ #

def test_contributions_are_exact_linear_decomposition(league, art):
    tip = last_tip(league) + timedelta(days=1)
    as_of = tip - timedelta(hours=1)
    sg = scheduled(6006, tip, 3, 4)
    fin, prof = _prod_inputs(art, league, [sg], as_of, "final")
    for t in spec.PRODUCTION_SPEC:
        X = inference.model_inputs(prof, fin, t)
        icpt, c = inference.linear_contributions(prof["models"][t]["estimator"], X)
        est = prof["models"][t]["estimator"]
        if t == "win":
            logit = np.log(est.predict_proba(X)[:, 1] / est.predict_proba(X)[:, 0])
            assert icpt + c.sum(axis=1)[0] == pytest.approx(logit[0], abs=1e-9)
        else:
            assert icpt + c.sum(axis=1)[0] == pytest.approx(est.predict(X)[0], abs=1e-9)
    p = inference.predict_games(art, league, [sg], [], as_of, kind="final")[0][0]
    fj = p.features_json
    json.dumps(fj, allow_nan=False)                                       # 合法 JSON（沒有 NaN）
    assert fj["contributions_unit"] == "log-odds" and len(fj["contributions"]) == inference.TOP_K
    assert all({"label", "value"} <= set(c) for c in fj["contributions"])   # 前端既有 contract
    assert "SHAP" in fj["target_contributions"]["margin"]["method"]          # 明示「不是 SHAP」
    for k in ("model_version", "artifact_version", "training_cutoff_utc", "feature_version", "data_quality"):
        assert fj[k]
    # 半場 / 全場一致性
    assert p.pred_home_h1 - p.pred_away_h1 == pytest.approx(fj["target_contributions"]["h1_margin"]["prediction"], abs=1e-3)
    assert (p.pred_home_h1 + p.pred_home_h2) - (p.pred_away_h1 + p.pred_away_h2) == pytest.approx(p.pred_margin)


def test_profile_selection_by_lead():
    assert spec.profile_for_lead(timedelta(minutes=60)) == "final"
    assert spec.profile_for_lead(timedelta(hours=2, minutes=59)) == "final"
    assert spec.profile_for_lead(timedelta(hours=3)) == "early"
    assert spec.profile_for_lead(timedelta(hours=20)) == "early"
