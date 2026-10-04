"""C.5D 端到端（暫存 schema）：排定比賽 → 未來特徵 → 載入 artifact → 預測 → 寫入 predictions → API 查詢讀得到；
冪等、傷病觸發 refresh、final 版本、不使用該場（假）結果。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.production import artifact as art_mod
from core.production import predict, retrain, spec
from prod_fixtures import add_scheduled, add_snapshot, last_tip, pid_of, scheduled, seed_db, snapshot, synthetic_league

# 與 src/db/queries.ts 的 getLatestPredictionForGame / getLatestPredictions 相同（? → %s）
API_LATEST_ONE = "SELECT * FROM predictions WHERE game_id = %s ORDER BY created_at DESC LIMIT 1"
API_LATEST_BATCH = """SELECT p.* FROM predictions p
      WHERE p.game_id IN (%s)
        AND p.created_at = (
          SELECT MAX(p2.created_at) FROM predictions p2 WHERE p2.game_id = p.game_id
        )"""


@pytest.fixture()
def world(db, tmp_path):
    league = synthetic_league(games_per_season=60)          # 60 場 / 季 → 每個 profile 120 筆 OOS 殘差（≥ MIN_DISTRIBUTION_FIT）
    tip = last_tip(league) + timedelta(days=1)
    sg = scheduled(9001, tip, 1, 2)
    other = scheduled(9002, tip + timedelta(hours=26), 3, 4)          # 36 小時視窗外（early 不會預測它）
    seed_db(db, league, [sg, other])
    res = retrain.retrain(last_tip(league) + timedelta(hours=5), root=tmp_path)    # 由 DB 載入訓練資料
    assert res.promoted and res.n_games == len(league.games)
    return {"league": league, "sg": sg, "tip": tip, "root": tmp_path, "db": db}


def _rows(db, gid):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM predictions WHERE game_id = %s ORDER BY created_at, id", (gid,))
        return cur.fetchall()


def test_end_to_end_scheduled_game_inference(world):
    db, sg, tip, root = world["db"], world["sg"], world["tip"], world["root"]
    now = tip - timedelta(hours=20)
    rep = predict.predict_upcoming_games_job("early", now, root=root)
    assert rep.candidates == 1 and rep.inserted == 1 and not rep.skipped
    with db.cursor() as cur:
        cur.execute(API_LATEST_ONE, (sg.game_id,))
        row = cur.fetchone()
        cur.execute(API_LATEST_BATCH % "%s", (sg.game_id,))
        assert len(cur.fetchall()) == 1
        cur.execute("SELECT last_status FROM data_sources WHERE source_key = 'model_predict'")
        assert cur.fetchone()["last_status"] == "ok"
    assert row["model_version"] == spec.MODEL_VERSION
    for k in ("home_win_prob", "pred_margin", "pred_total", "pred_home_h1", "pred_away_h1", "pred_home_h2",
              "pred_away_h2", "confidence"):
        assert row[k] is not None
    assert 0 < row["home_win_prob"] < 1 and 150 < row["pred_total"] < 300
    assert row["pred_home_h1"] + row["pred_home_h2"] - row["pred_away_h1"] - row["pred_away_h2"] == pytest.approx(row["pred_margin"])
    fj = row["features_json"]
    assert fj["prediction_kind"] == "early" and fj["profile"] == "early"
    assert fj["artifact_version"] == art_mod.current_version(root)
    assert fj["contributions"] and all("label" in c and "value" in c for c in fj["contributions"])
    assert fj["training_cutoff_utc"] and fj["data_quality"]["prediction_allowed"] is True

    # 重跑：輸入沒變 → 不新增
    rep2 = predict.predict_upcoming_games_job("early", now + timedelta(minutes=30), root=root)
    assert rep2.inserted == 0 and rep2.unchanged == 1
    assert len(_rows(db, sg.game_id)) == 1

    # 開賽前 60 分鐘：final 版本（profile 換成 final）寫入一次；之後重跑不再寫
    rep3 = predict.predict_upcoming_games_job("final", tip - timedelta(minutes=60), root=root)
    assert rep3.inserted == 1
    rows = _rows(db, sg.game_id)
    assert [r["features_json"]["prediction_kind"] for r in rows] == ["early", "final"]
    assert rows[-1]["features_json"]["profile"] == "final" and rows[-1]["features_json"]["supersedes"] == rows[0]["id"]
    rep4 = predict.predict_upcoming_games_job("final", tip - timedelta(minutes=55), root=root)
    assert rep4.candidates == 0 and len(_rows(db, sg.game_id)) == 2
    # 開賽後不再寫
    rep5 = predict.predict_upcoming_games_job("early", tip + timedelta(minutes=1), root=root, game_ids=[sg.game_id])
    assert rep5.candidates == 0 and len(_rows(db, sg.game_id)) == 2


def test_fake_result_on_scheduled_game_is_never_used(world):
    """排定比賽的列上就算出現（錯誤的）比分，只要不是 final 就不會進入任何特徵：input_hash 相同。"""
    db, sg, tip, root = world["db"], world["sg"], world["tip"], world["root"]
    now = tip - timedelta(hours=20)
    a = predict.predict_upcoming_games_job("early", now, root=root, dry_run=True).predictions[0]
    with db.cursor() as cur:
        cur.execute("UPDATE games SET home_pts = 150, away_pts = 60 WHERE id = %s", (sg.game_id,))
        # 預測時間戳之後才有的資料：一場「未來」已 final 的比賽 + 預測時間戳之後的傷病報告
        add_scheduled(cur, scheduled(9100, tip + timedelta(hours=1), 1, 3), status="final", home_pts=140, away_pts=80)
        add_snapshot(cur, snapshot(77777, now + timedelta(minutes=5), tip, [1], {1: [(pid_of(1, 0, 2), "Out")]}))
    b = predict.predict_upcoming_games_job("early", now, root=root, dry_run=True).predictions[0]
    assert a == b


def test_prediction_idempotency_under_reruns(world):
    db, sg, tip, root = world["db"], world["sg"], world["tip"], world["root"]
    now = tip - timedelta(hours=20)
    for k in range(3):
        predict.predict_upcoming_games_job("early", now + timedelta(minutes=k), root=root)
        predict.predict_upcoming_games_job("refresh", now + timedelta(minutes=k), root=root)
    assert len(_rows(db, sg.game_id)) == 1


def test_refresh_after_injury_change(world):
    db, sg, tip, root = world["db"], world["sg"], world["tip"], world["root"]
    now = tip - timedelta(hours=20)
    with db.cursor() as cur:            # 前一晚的報告：兩隊皆已申報、無人列入
        add_snapshot(cur, snapshot(80000, now - timedelta(minutes=30), tip, [1, 2], {1: [], 2: []}))
    predict.predict_upcoming_games_job("early", now, root=root)
    base = _rows(db, sg.game_id)[-1]
    assert base["features_json"]["injuries"]["home"]["known"] is True
    # 沒有新報告 → refresh 不是候選
    r0 = predict.predict_upcoming_games_job("refresh", now + timedelta(minutes=15), root=root)
    assert r0.candidates == 0
    # 新報告但內容相同（仍無人列入）→ 重算、輸入沒變 → 不寫
    with db.cursor() as cur:
        add_snapshot(cur, snapshot(80001, now + timedelta(minutes=20), tip, [1, 2], {1: [], 2: []}))
    r1 = predict.predict_upcoming_games_job("refresh", now + timedelta(minutes=30), root=root)
    assert r1.candidates == 1 and r1.inserted == 0
    # 主隊主力被列 Out → 實質變化 → 寫入 refresh 版本
    with db.cursor() as cur:
        add_snapshot(cur, snapshot(80002, now + timedelta(minutes=40), tip, [1, 2],
                                   {1: [(pid_of(1, 0, 2), "Out")], 2: []}))
    r2 = predict.predict_upcoming_games_job("refresh", now + timedelta(minutes=45), root=root)
    assert r2.inserted == 1
    new = _rows(db, sg.game_id)[-1]
    assert new["features_json"]["prediction_kind"] == "refresh"
    assert new["home_win_prob"] < base["home_win_prob"]
    assert new["features_json"]["injuries"]["home"]["n_out"] == 1
    assert new["features_json"]["supersedes"] == base["id"]
    # 之後沒有新報告 → 不再是候選
    r3 = predict.predict_upcoming_games_job("refresh", now + timedelta(minutes=60), root=root)
    assert r3.candidates == 0 and len(_rows(db, sg.game_id)) == 2


def test_missing_artifact_fails_clearly_and_writes_heartbeat(db, tmp_path):
    with pytest.raises(art_mod.NoProductionArtifact):
        predict.predict_upcoming_games_job("early", root=tmp_path)
    with db.cursor() as cur:
        cur.execute("SELECT last_status, last_error FROM data_sources WHERE source_key = 'model_predict'")
        r = cur.fetchone()
    assert r["last_status"] == "error" and "run_retrain" in r["last_error"]


def test_artifact_switch_produces_new_tracked_version(world):
    """重訓 promote 新版本後，同一場比賽的下一次預測會寫入新的一筆（artifact_version 不同），舊的保留；可回滾。"""
    db, sg, tip, root, league = world["db"], world["sg"], world["tip"], world["root"], world["league"]
    now = tip - timedelta(hours=20)
    predict.predict_upcoming_games_job("early", now, root=root)
    v1 = art_mod.current_version(root)
    retrain.retrain(last_tip(league) + timedelta(hours=6), root=root)
    v2 = art_mod.current_version(root)
    assert v1 != v2
    predict.predict_upcoming_games_job("refresh", now + timedelta(minutes=5), root=root)
    rows = _rows(db, sg.game_id)
    assert [r["features_json"]["artifact_version"] for r in rows] == [v1, v2]
    art_mod.rollback(root)
    assert art_mod.current_version(root) == v1


def test_line_probability_from_stored_prediction_row(world):
    """C.5E：API 讀到的那一列 → 以當時 artifact 版本重算任意盤口線機率（決定性）。"""
    from core.production import probability
    db, sg, tip, root = world["db"], world["sg"], world["tip"], world["root"]
    predict.predict_upcoming_games_job("early", tip - timedelta(hours=20), root=root)
    with db.cursor() as cur:
        cur.execute(API_LATEST_ONE, (sg.game_id,))
        row = cur.fetchone()
    fj = row["features_json"]
    assert set(fj["predictive_distributions"]) == set(spec.DISTRIBUTION_TARGETS)
    lines = {"margin": [-4.5, 0, 3], "total": [round(row["pred_total"]) + 0.5, round(row["pred_total"])],
             "h1_margin": [0, 1.5], "h1_total": [110.5, 111]}
    for target, ls in lines.items():
        for L in ls:
            a = probability.line_probability_from_prediction_row(row, target, L, root=root)
            b = probability.line_probability_from_prediction_row(row, target, L, root=root)
            assert a == b and a["artifact_version"] == fj["artifact_version"]
            assert a["probability_above"] + a["probability_push"] + a["probability_below"] == pytest.approx(1.0)
