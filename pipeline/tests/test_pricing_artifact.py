"""Phase D.2：以真實（合成資料訓練的）artifact 驗證 production 機率路徑 + DB 持久化 / 冪等 / 歷史重建

- 模型機率必須等於 C.5E probability.py 的輸出（不另算 normal CDF）
- 歷史預測用該列記錄的 artifact 版本（即使 CURRENT 已換版）
- market_pricing_snapshots：決定性、冪等、不改寫；live 與重建只用 as_of 以前的資料
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.odds.canonical import FULL_GAME, H1, MONEYLINE, OPEN, SPREAD, SUSPENDED, THREE_WAY, TOTAL, make_snapshot
from core.odds.store import persist_snapshots
from core.pricing import engine
from core.pricing.alignment import odds_input_from_row
from core.pricing.engine import PRICED, OddsInput, RowModelProbabilities, prediction_from_row, price_market
from core.pricing.job import pricing_job, reconstruct_game
from core.production import artifact as art_mod
from core.production import inference, probability, retrain, spec
from prod_fixtures import last_tip, scheduled, synthetic_league

UTC = timezone.utc


@pytest.fixture(scope="module")
def league():
    return synthetic_league()


@pytest.fixture(scope="module")
def setup(league, tmp_path_factory):
    """root 內兩個版本：A（訓練得到）與 B（A 的分差 σ × 1.5，CURRENT）。"""
    cut = last_tip(league) + timedelta(hours=5)
    hist = league.before(cut - spec.TRAINING_GAME_BUFFER)
    b, f = retrain.train_bundle(hist, cut, trained_at=datetime(2026, 10, 4, tzinfo=UTC))
    root = tmp_path_factory.mktemp("arts")
    art_mod.save_version(b, root, gates=retrain.run_gates(b, f), verify=retrain.roundtrip_verifier(b, f))
    b2 = copy.deepcopy(b)
    b2["artifact_version"] = b["artifact_version"] + "-B"
    for p in spec.PROFILES:
        b2["profiles"][p]["distributions"]["margin"]["state"]["sigma"] *= 1.5
    art_mod.save_version(b2, root, gates={"passed": True})
    art_mod.promote(b["artifact_version"], root)
    art_mod.promote(b2["artifact_version"], root)
    art_a = art_mod.load_version(b["artifact_version"], root)
    tip = last_tip(league) + timedelta(days=1)
    gp = inference.predict_games(art_a, league, [scheduled(9001, tip, 1, 2)], [], tip - timedelta(hours=1),
                                 kind="final")[0][0]
    return {"root": root, "art_a": art_a, "version_b": b2["artifact_version"], "gp": gp}


def pred_row(gp, *, pid=1, game_id=9001, created=datetime(2026, 10, 21, 20, 0, tzinfo=UTC), kind="final"):
    fj = dict(gp.features_json)
    fj["prediction_kind"] = kind
    fj["prediction_as_of_utc"] = created.isoformat()
    return {"id": pid, "game_id": game_id, "model_version": spec.MODEL_VERSION, "created_at": created,
            "home_win_prob": gp.home_win_prob, "pred_margin": gp.pred_margin, "pred_total": gp.pred_total,
            "pred_home_h1": gp.pred_home_h1, "pred_away_h1": gp.pred_away_h1, "features_json": fj}


def S(mt, period=FULL_GAME, status=OPEN, outcome_set="two_way", **kw):
    return make_snapshot(source="twsport", bookmaker="twsport", source_event_id="E", market_type=mt, period=period,
                         status=status, outcome_set=outcome_set, **kw)


def price(setup, snap, row=None):
    row = row or pred_row(setup["gp"])
    p = prediction_from_row(row)
    model = RowModelProbabilities(p, root=setup["root"])
    return price_market(OddsInput(1, 9001, p.created_at, snap), p, model, as_of=p.created_at + timedelta(minutes=1))


def side(mp, s):
    return next(o for o in mp.outcomes if o.side == s)


# ---------------- production 機率路徑 = C.5E probability.py ---------------- #

def test_model_probabilities_come_from_c5e_interface(setup):
    gp, art = setup["gp"], setup["art_a"]
    m = probability.predict_margin_probability
    cases = [
        (S(MONEYLINE, prices={"home": 1.8, "away": 2.0}), "home", m(gp, 0, art)["probability_above"]),
        (S(MONEYLINE, prices={"home": 1.8, "away": 2.0}), "away", m(gp, 0, art)["probability_below"]),
        (S(SPREAD, home_line=-5.5, prices={"home": 1.9, "away": 1.9}), "home", m(gp, 5.5, art)["probability_above"]),
        (S(SPREAD, home_line=-5.5, prices={"home": 1.9, "away": 1.9}), "away", m(gp, 5.5, art)["probability_below"]),
        (S(SPREAD, home_line=3.5, prices={"home": 1.9, "away": 1.9}), "home", m(gp, -3.5, art)["probability_above"]),
        (S(TOTAL, total_line=221.5, prices={"over": 1.9, "under": 1.9}), "over",
         probability.predict_total_probability(gp, 221.5, art)["probability_above"]),
        (S(TOTAL, total_line=221.5, prices={"over": 1.9, "under": 1.9}), "under",
         probability.predict_total_probability(gp, 221.5, art)["probability_below"]),
        (S(SPREAD, H1, home_line=-1.5, prices={"home": 1.9, "away": 1.9}), "home",
         probability.predict_h1_margin_probability(gp, 1.5, art)["probability_above"]),
        (S(TOTAL, H1, total_line=110.5, prices={"over": 1.9, "under": 1.9}), "under",
         probability.predict_h1_total_probability(gp, 110.5, art)["probability_below"]),
        (S(MONEYLINE, H1, outcome_set=THREE_WAY, prices={"home": 1.7, "draw": 10.0, "away": 1.9}), "draw",
         probability.predict_h1_margin_probability(gp, 0, art)["probability_push"]),
    ]
    for snap, s, expected in cases:
        mp = price(setup, snap)
        assert mp.status == PRICED and mp.distribution_version == spec.DISTRIBUTION_VERSION
        assert mp.artifact_version == art.artifact_version and mp.model_version == spec.MODEL_VERSION
        assert side(mp, s).model_prob == expected                                        # 逐位元相同


def test_real_distribution_push_and_ev_semantics(setup):
    gp = setup["gp"]
    k = float(round(gp.pred_margin)) or 1.0                                               # 讓分 0（pk）：全場分差不會是 0 → push 0
    mp = price(setup, S(SPREAD, home_line=-k, prices={"home": 1.91, "away": 1.91}))
    h, a = side(mp, "home"), side(mp, "away")
    assert mp.settlement_rule == "integer_line_push_refund" and h.push_prob > 0 and h.push_prob == a.push_prob
    for o in (h, a):
        assert o.model_prob + o.push_prob + o.loss_prob == pytest.approx(1.0, abs=1e-12)
        assert o.ev_per_unit == pytest.approx(o.model_prob * 1.91 + o.push_prob - 1, abs=1e-12)
    ml = price(setup, S(MONEYLINE, prices={"home": 1.8, "away": 2.0}))
    assert all(o.push_prob == 0.0 for o in ml.outcomes)                                   # 全場分差不會平手
    assert side(ml, "home").model_prob + side(ml, "away").model_prob == pytest.approx(1.0, abs=1e-12)
    half = price(setup, S(SPREAD, home_line=-k - 0.5, prices={"home": 1.91, "away": 1.91}))
    assert all(o.push_prob == 0.0 for o in half.outcomes)
    h1 = price(setup, S(MONEYLINE, H1, outcome_set=THREE_WAY, prices={"home": 1.7, "draw": 10.0, "away": 1.9}))
    assert sum(o.model_prob for o in h1.outcomes) == pytest.approx(1.0, abs=1e-12)
    assert all(o.push_prob == 0.0 for o in h1.outcomes)


def test_ml_consistency_uses_stored_logistic_probability(setup):
    mp = price(setup, S(MONEYLINE, prices={"home": 1.8, "away": 2.0}))
    c = mp.diagnostics["ml_consistency"]
    assert c["logistic_home_win_prob"] == setup["gp"].home_win_prob
    assert c["abs_diff"] == pytest.approx(abs(side(mp, "home").model_prob - setup["gp"].home_win_prob))
    assert c["warning"] == (c["abs_diff"] > 0.05)


def test_stored_artifact_version_is_used_not_current(setup):
    root, gp = setup["root"], setup["gp"]
    assert art_mod.current_version(root) == setup["version_b"]                           # CURRENT 已換版
    mp = price(setup, S(SPREAD, home_line=-4.5, prices={"home": 1.9, "away": 1.9}))
    a = probability.predict_margin_probability(gp, 4.5, setup["art_a"])["probability_above"]
    b = probability.predict_line_probability(art_mod.load_current(root), "final", "margin", gp.pred_margin, 4.5)
    assert side(mp, "home").model_prob == a and a != b["probability_above"]
    assert mp.artifact_version == setup["art_a"].artifact_version
    with pytest.raises(ValueError):                                                     # 傳錯版本的 artifact → 拒絕
        probability.line_probability_from_prediction_row(pred_row(gp), "margin", 4.5, art=art_mod.load_current(root))
    missing = pred_row(gp)
    missing["features_json"] = {**missing["features_json"], "artifact_version": "ml-v2.0+missing"}
    with pytest.raises(engine.ModelProbabilityError):
        RowModelProbabilities(prediction_from_row(missing), root=root)


# ---------------- DB：持久化 / 冪等 / 重建 ---------------- #

TIP = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)


@pytest.fixture()
def game(db, seeded):
    with db.cursor() as cur:
        return db.upsert_game(cur, nba_game_id="0022600099", season="2026-27", season_stage="regular",
                              date_utc=TIP, home_team_id=seeded["BOS"], away_team_id=seeded["NYK"], status="scheduled")


def write_odds(db, gid, snap, at):
    with db.transaction() as cur:
        persist_snapshots(cur, [(gid, snap)], source=snap.source, fetched_at=at)


def insert_pred(db, gid, setup, created, kind="early"):
    r = pred_row(setup["gp"], game_id=gid, created=created, kind=kind)
    with db.cursor() as cur:
        cur.execute("""INSERT INTO predictions (game_id, model_version, created_at, home_win_prob, pred_margin,
                         pred_total, pred_home_h1, pred_away_h1, features_json)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id""",
                    (gid, r["model_version"], created, r["home_win_prob"], r["pred_margin"], r["pred_total"],
                     r["pred_home_h1"], r["pred_away_h1"], json.dumps(r["features_json"])))
        return cur.fetchone()["id"]


def pricing_rows(db, gid):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM market_pricing_snapshots WHERE game_id = %s ORDER BY id", (gid,))
        return cur.fetchall()


def test_pricing_job_persists_deterministically_and_idempotently(db, game, setup):
    t1 = TIP - timedelta(hours=10)
    pid = insert_pred(db, game, setup, t1 - timedelta(hours=1))
    write_odds(db, game, S(MONEYLINE, prices={"home": 1.8, "away": 2.0}), t1)
    write_odds(db, game, S(SPREAD, home_line=-4.0, prices={"home": 1.91, "away": 1.91}), t1)
    write_odds(db, game, make_snapshot(source="oddsapi", bookmaker="draftkings", source_event_id="X",
                                       market_type=TOTAL, period=FULL_GAME, status=OPEN, total_line=221.5,
                                       prices={"over": 1.91, "under": 1.91}), t1)
    now = t1 + timedelta(minutes=5)
    rep = pricing_job(now, root=setup["root"], db=db)
    assert rep.errors == [] and rep.markets == 3 and rep.by_status == {PRICED: 3} and rep.rows_inserted == 6
    rows = pricing_rows(db, game)
    assert {r["prediction_id"] for r in rows} == {pid} and {r["bookmaker"] for r in rows} == {"twsport", "draftkings"}
    assert all(r["analysis_as_of"] == t1 for r in rows) and all(r["no_vig_method"] == "proportional-v1" for r in rows)
    rep2 = pricing_job(now + timedelta(minutes=5), root=setup["root"], db=db)
    assert rep2.rows_inserted == 0 and rep2.rows_existing == 6 and len(pricing_rows(db, game)) == 6
    # 存的值 = 以同樣輸入重算的值
    with db.cursor() as cur:
        cur.execute("SELECT * FROM odds_snapshots WHERE game_id = %s AND market = 'spread'", (game,))
        orow = cur.fetchone()
        cur.execute("SELECT * FROM predictions WHERE id = %s", (pid,))
        prow = cur.fetchone()
    p = prediction_from_row(prow)
    mp = price_market(odds_input_from_row(orow), p, RowModelProbabilities(p, root=setup["root"]), as_of=now)
    stored = {r["side"]: r for r in rows if r["market"] == "spread"}
    for o in mp.outcomes:
        for k in ("model_prob", "push_prob", "fair_no_vig_prob", "edge_vs_fair", "ev_per_unit", "raw_implied_prob"):
            # DB 回讀的 float8 在這條連線上是 15 位有效數字（extra_float_digits=0）→ 以 1e-12 相對誤差比較
            assert stored[o.side][k] == pytest.approx(getattr(o, k), rel=1e-12, abs=1e-15)


def test_market_only_then_priced_when_prediction_arrives(db, game, setup):
    t1 = TIP - timedelta(hours=30)
    write_odds(db, game, S(MONEYLINE, prices={"home": 1.8, "away": 2.0}), t1)
    rep = pricing_job(t1 + timedelta(minutes=1), root=setup["root"], db=db)
    assert rep.by_status == {"market_only": 1}
    pid = insert_pred(db, game, setup, t1 + timedelta(hours=2))
    rep = pricing_job(t1 + timedelta(hours=2, minutes=5), root=setup["root"], db=db)
    assert rep.by_status == {PRICED: 1} and rep.rows_inserted == 2
    rows = pricing_rows(db, game)
    assert [r["prediction_id"] for r in rows] == [None, None, pid, pid]                  # 舊列保留、不改寫
    assert rows[2]["analysis_as_of"] == t1 + timedelta(hours=2)
    assert rows[0]["model_prob"] is None and rows[2]["model_prob"] is not None


def test_non_open_latest_snapshot_is_not_priced(db, game, setup):
    t1 = TIP - timedelta(hours=10)
    insert_pred(db, game, setup, t1 - timedelta(hours=1))
    write_odds(db, game, S(MONEYLINE, prices={"home": 1.8, "away": 2.0}), t1)
    write_odds(db, game, S(MONEYLINE, status=SUSPENDED, prices={"home": None, "away": None}), t1 + timedelta(hours=1))
    rep = pricing_job(t1 + timedelta(hours=1, minutes=5), root=setup["root"], db=db)
    assert rep.markets == 0 and rep.skipped_not_open == 1 and pricing_rows(db, game) == []


def test_reconstruction_uses_only_data_available_at_as_of(db, game, setup):
    t = TIP - timedelta(hours=12)
    p_early = insert_pred(db, game, setup, t - timedelta(hours=1), kind="early")
    write_odds(db, game, S(SPREAD, home_line=-3.5, prices={"home": 1.9, "away": 1.9}), t)
    p_final = insert_pred(db, game, setup, TIP - timedelta(hours=1), kind="final")
    write_odds(db, game, S(SPREAD, home_line=-6.5, prices={"home": 1.9, "away": 1.9}), TIP - timedelta(minutes=30))
    early = reconstruct_game(game, t + timedelta(hours=1), root=setup["root"], db=db).pricings
    assert len(early) == 1 and early[0]["prediction_id"] == p_early and early[0]["line"] == -3.5
    late = reconstruct_game(game, TIP - timedelta(minutes=10), root=setup["root"], db=db).pricings
    assert late[0]["prediction_id"] == p_final and late[0]["line"] == -6.5
    assert pricing_rows(db, game) == []                                                  # 重建不寫入
    again = reconstruct_game(game, t + timedelta(hours=1), root=setup["root"], db=db).pricings
    assert again == early                                                                # 之後的資料不改變較早的分析
