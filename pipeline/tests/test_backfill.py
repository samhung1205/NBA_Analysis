"""五季 box score 回填：認領 ESPN 列、冪等、續傳、單場失敗不中止、封鎖偵測、玩家球隊與處理順序無關"""
import pytest

from core.jobs import box_backfill as bb
from core.sources import nba_cdn
from tests.helpers import BOS, LAL, NYK, add_espn_game, count, lineup, make_box, player, team_raw, utc

G1, G2, G3 = "0022100001", "0022100002", "0022100003"


@pytest.fixture()
def run(db, seeded, monkeypatch):
    """回傳 run(boxes, **kw)：boxes = {gid: box | 'missing' | 'error'}；只枚舉 boxes 內的 ID，不睡覺。"""
    def go(boxes, ids=None, **kw):
        ids = list(boxes) if ids is None else ids
        monkeypatch.setattr(bb, "candidate_ids", lambda season, stages=(): ids)

        def fetch(gid):
            v = boxes.get(gid, "missing")
            if v == "missing":
                return "missing", None, "403"
            if v == "error":
                return "error", None, "ReadTimeout: boom"
            return "ok", v, None

        kw.setdefault("sleep", lambda s: None)
        kw.setdefault("workers", 2)
        return bb.backfill_season("2021-22", fetch=fetch, **kw)
    return go


def test_candidate_ids_cover_known_id_schemes():
    ids = bb.candidate_ids("2023-24")
    assert "0022300001" in ids and "0022301230" in ids and "0062300001" in ids       # 例行賽 / Cup 決賽
    assert "0042300101" in ids and "0042300307" in ids and "0042300401" in ids       # 季後賽
    assert {"0052300101", "0052300131", "0052300211"} <= set(ids)                    # 附加賽（每季 6 場）
    assert len([i for i in ids if i.startswith("005")]) == 6
    assert len(ids) == len(set(ids))
    assert "0062200001" not in bb.candidate_ids("2022-23")                           # Cup 決賽自 2023-24 才有
    assert bb.candidate_ids("2021-22", ("regular",))[-1] == "0022101230"


def test_adopts_espn_rows_writes_everything_and_is_idempotent(db, seeded, run):
    add_espn_game(db, seeded, "a", utc(2021, 10, 20, 0))
    add_espn_game(db, seeded, "b", utc(2021, 10, 22, 0), home="NYK", away="BOS")
    boxes = {G1: make_box(G1, utc(2021, 10, 20, 0), BOS, NYK), G2: make_box(G2, utc(2021, 10, 22, 0), NYK, BOS)}

    st = run(boxes)
    assert (st.stored, st.adopted, st.inserted, st.errors) == (2, 2, 0, 0)
    assert count(db, "games") == 2                                    # 認領，不是新增
    assert count(db, "games", "nba_game_id LIKE 'espn:%%'") == 0
    assert count(db, "team_game_stats", "fga IS NOT NULL") == 4
    assert count(db, "team_game_derived") == 4
    assert count(db, "player_game_stats") == 32                       # 2 場 × 2 隊 × 8 人
    assert count(db, "player_game_stats", "started = 1") == 20

    snapshot = {t: count(db, t) for t in ("games", "team_game_stats", "team_game_derived", "player_game_stats",
                                           "players")}
    st2 = run(boxes)                                                  # 重跑
    assert st2.stored == 0 and st2.skipped_done == 2
    assert snapshot == {t: count(db, t) for t in snapshot}            # 沒有重複資料
    st3 = run(boxes, ids=[G1, G2])
    assert st3.stored == 0


def test_inserts_game_that_espn_never_had(db, seeded, run):
    st = run({G1: make_box(G1, utc(2021, 10, 20, 0), BOS, NYK)})
    assert (st.stored, st.adopted, st.inserted) == (1, 0, 1)
    with db.cursor() as cur:
        cur.execute("SELECT season, season_stage, status, home_pts, home_q1, home_h1 FROM games")
        assert cur.fetchone() == {"season": "2021-22", "season_stage": "regular", "status": "final",
                                  "home_pts": 110, "home_q1": 27, "home_h1": 54}


def test_back_to_back_same_matchup_each_adopts_its_own_row(db, seeded, run):
    """回歸：同一對主客隊連續兩天主場對打，舊的 ±1 天認領會同時命中兩列而違反 UNIQUE(nba_game_id)。"""
    r1 = add_espn_game(db, seeded, "n1", utc(2021, 11, 27, 2), hpts=97, apts=98)
    r2 = add_espn_game(db, seeded, "n2", utc(2021, 11, 28, 2), hpts=127, apts=105)
    boxes = {G1: make_box(G1, utc(2021, 11, 27, 2), BOS, NYK, hpts=97, apts=98),
             G2: make_box(G2, utc(2021, 11, 28, 2), BOS, NYK, hpts=127, apts=105)}
    st = run(boxes)
    assert (st.stored, st.errors, st.adopted) == (2, 0, 2)
    with db.cursor() as cur:
        cur.execute("SELECT id, nba_game_id FROM games ORDER BY date_utc")
        assert [(r["id"], r["nba_game_id"]) for r in cur.fetchall()] == [(r1, G1), (r2, G2)]


def test_single_game_error_does_not_abort_season_and_resume_completes(db, seeded, run):
    for k, d in (("a", 20), ("b", 21), ("c", 22)):
        add_espn_game(db, seeded, k, utc(2021, 10, d, 0))
    good = {G1: make_box(G1, utc(2021, 10, 20, 0), BOS, NYK), G3: make_box(G3, utc(2021, 10, 22, 0), BOS, NYK)}

    st = run({**good, G2: "error"}, ids=[G1, G2, G3])
    assert (st.stored, st.errors, st.failed_ids) == (2, 1, [G2])      # G2 失敗，其餘照常
    assert count(db, "source_fetch_log", "status = 'error' AND key = %s", (G2,)) == 1

    st = run({**good, G2: make_box(G2, utc(2021, 10, 21, 0), BOS, NYK)}, ids=[G1, G2, G3])   # 續傳
    assert (st.stored, st.skipped_done, st.errors) == (1, 2, 0)
    assert count(db, "games", "nba_game_id LIKE 'espn:%%'") == 0
    assert count(db, "team_game_stats") == 6 and count(db, "team_game_derived") == 6
    assert count(db, "source_fetch_log", "status = 'ok'") == 3


def test_write_failure_mid_game_rolls_back_and_resume_recovers(db, seeded, run, monkeypatch):
    """寫入中途失敗（例如 DB 斷線）：整場回滾，不留下半套資料；ESPN 列也維持未認領，重跑可完整補上。"""
    add_espn_game(db, seeded, "a", utc(2021, 10, 20, 0))
    box = make_box(G1, utc(2021, 10, 20, 0), BOS, NYK)

    import core.jobs.box_store as bs
    real = bs.store_derived
    monkeypatch.setattr(bs, "store_derived", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection lost")))
    st = run({G1: box})
    assert st.stored == 0 and st.errors == 1 and st.failed_ids == [G1]
    with db.cursor() as cur:
        cur.execute("SELECT last_error FROM source_fetch_log WHERE key = %s", (G1,))
        assert "connection lost" in cur.fetchone()["last_error"]
    assert count(db, "team_game_stats") == 0 and count(db, "player_game_stats") == 0
    assert count(db, "games", "nba_game_id = 'espn:a'") == 1          # 認領也一併回滾
    assert count(db, "source_fetch_log", "status = 'error'") == 1

    monkeypatch.setattr(bs, "store_derived", real)
    st = run({G1: box})
    assert st.stored == 1 and count(db, "team_game_stats") == 2 and count(db, "games", "nba_game_id = %s", (G1,)) == 1


def test_missing_ids_logged_and_not_reprobed_unless_retry(db, seeded, run):
    st = run({}, ids=[G1, G2])
    assert st.missing == 2 and count(db, "source_fetch_log", "status = 'missing'") == 2
    st = run({}, ids=[G1, G2])
    assert st.skipped_logged_missing == 2 and st.missing == 0
    box = make_box(G1, utc(2021, 10, 20, 0), BOS, NYK)
    st = run({G1: box}, ids=[G1, G2], retry_missing=True)             # 之後該檔案出現了
    assert st.stored == 1 and st.missing == 1


def test_not_final_box_is_not_stored(db, seeded, run):
    st = run({G1: make_box(G1, utc(2021, 10, 20, 0), BOS, NYK, status="live")})
    assert st.not_final == 1 and st.stored == 0 and count(db, "team_game_stats") == 0


def test_unmapped_team_counts_as_failure_not_crash(db, seeded, run):
    st = run({G1: make_box(G1, utc(2021, 10, 20, 0), 999, NYK)})
    assert st.stored == 0 and st.unmapped == 1 and st.failed_ids == [G1]


def test_block_detection_aborts_without_poisoning_missing_log(db, seeded, run):
    ids = [f"00221{n:05d}" for n in range(100, 130)]
    sleeps = []
    st = run({}, ids=ids, workers=2, block_streak=6, backoff_seconds=(1, 2), sleep=sleeps.append)
    assert st.aborted and sleeps == [1, 2]                            # canary 一直失敗 → 退避後放棄
    assert st.missing < len(ids)                                      # 沒有硬把整季掃完
    # 被誤記的 missing（其實是被限流）已清掉，重跑不會因此跳過
    assert st.missing_ids and count(db, "source_fetch_log", "status = 'missing' AND key = ANY(%s)",
                                    (st.missing_ids[-12:],)) == 0


def test_block_detection_with_healthy_canary_just_continues(db, seeded, run, monkeypatch):
    ids = [f"00221{n:05d}" for n in range(100, 120)]
    canary_box = make_box(bb.CANARY_ID, utc(2021, 10, 19, 23), BOS, NYK)
    boxes = {bb.CANARY_ID: canary_box}
    st = run(boxes, ids=ids, block_streak=5)
    assert st.aborted is None and st.missing == 20                    # canary 正常 → 這些就是真的不存在


def test_player_current_team_is_independent_of_processing_order(db, seeded, run):
    """球員 7 在舊賽季效力 BOS、新賽季效力 LAL。先處理新的再處理舊的（亂序重跑），目前球隊仍應是 LAL。"""
    add_espn_game(db, seeded, "old", utc(2021, 10, 20, 0), home="BOS", away="NYK")
    add_espn_game(db, seeded, "new", utc(2022, 3, 1, 0), home="LAL", away="NYK")
    mover_old = lineup(1)[:7] + [player(777, "Mover Guy", True, 20.0)]
    mover_new = lineup(3)[:7] + [player(777, "Mover Guy", True, 25.0)]
    boxes = {G1: make_box(G1, utc(2021, 10, 20, 0), BOS, NYK, home_players=mover_old),
             G2: make_box(G2, utc(2022, 3, 1, 0), LAL, NYK, home_players=mover_new)}
    run(boxes, ids=[G2, G1])
    with db.cursor() as cur:
        db.refresh_player_current_team(cur)
        cur.execute("SELECT team_id FROM players WHERE nba_player_id = 777")
        assert cur.fetchone()["team_id"] == seeded["LAL"]
        assert db.refresh_player_current_team(cur) == 0               # 冪等


def test_cdn_rebounds_are_player_only_and_turnovers_include_team():
    """官方球隊 REB/OREB/DREB 不含「球隊籃板」；TOV 含「球隊失誤」。
    （CDN 的 reboundsTotal 含球隊籃板：2021-22 平均 52.4 vs 官方 ~44，曾使 pace 低估 4%。）"""
    side = {"statistics": {"points": 127, "reboundsOffensive": 13, "reboundsTeamOffensive": 7,
                           "reboundsDefensive": 41, "reboundsTeamDefensive": 1, "reboundsPersonal": 54,
                           "reboundsTeam": 8, "reboundsTotal": 62,
                           "turnovers": 7, "turnoversTeam": 1, "turnoversTotal": 8, "fieldGoalsMade": 48,
                           "fieldGoalsAttempted": 105, "threePointersMade": 17, "threePointersAttempted": 45,
                           "freeThrowsMade": 14, "freeThrowsAttempted": 18, "minutes": "PT240M00.00S"},
            "players": [], "periods": []}
    t = nba_cdn.shape_box_side(side)["team"]
    assert (t["oreb"], t["dreb"], t["reb"]) == (13, 41, 54)
    assert t["oreb"] + t["dreb"] == t["reb"]
    assert (t["tov"], t["team_min"]) == (8, 240.0)
    assert (t["fgm"], t["fga"], t["fg3m"], t["ftm"], t["fta"]) == (48, 105, 17, 14, 18)


def test_derive_job_fills_missing_and_recomputes_on_version_change(db, seeded):
    from core import metrics
    from core.jobs.derive_metrics import derive_all
    from core.jobs.games_sync import _store_basic
    gid = add_espn_game(db, seeded, "a", utc(2021, 10, 20, 0))
    game = {"id": gid, "home_team_id": seeded["BOS"], "away_team_id": seeded["NYK"]}
    box = make_box("0022100001", utc(2021, 10, 20, 0), BOS, NYK)
    with db.cursor() as cur:
        _store_basic(cur, game, box)
        cur.execute("DELETE FROM team_game_derived")                  # 模擬「只有原始計數、缺衍生指標」
        assert derive_all(cur) == {"derived": 1, "skipped_incomplete_raw": 0}
        assert derive_all(cur)["derived"] == 0                        # 已是目前版本 → 不重算
        cur.execute("UPDATE team_game_derived SET formula_version = 'box-v0'")
        assert derive_all(cur)["derived"] == 1                        # 版本不符 → 重算
        cur.execute("SELECT DISTINCT formula_version FROM team_game_derived")
        assert [r["formula_version"] for r in cur.fetchall()] == [metrics.FORMULA_VERSION]
        # 原始計數不完整 → 不補造
        cur.execute("DELETE FROM team_game_derived")
        cur.execute("UPDATE team_game_stats SET fga = NULL WHERE team_id = %s", (seeded["BOS"],))
        assert derive_all(cur)["derived"] == 0
        assert count(db, "team_game_derived") == 0


def test_deadlock_is_retried_and_game_is_stored(db, seeded, run, monkeypatch):
    from psycopg import errors as pgerr
    add_espn_game(db, seeded, "a", utc(2021, 10, 20, 0))
    calls = {"n": 0}
    real = bb.store_one

    def flaky(box, team_map):
        calls["n"] += 1
        if calls["n"] < 3:
            raise pgerr.DeadlockDetected("deadlock detected")
        return real(box, team_map)

    monkeypatch.setattr(bb.time, "sleep", lambda s: None)
    monkeypatch.setattr(bb, "store_one", flaky)
    st = run({G1: make_box(G1, utc(2021, 10, 20, 0), BOS, NYK)}, store=flaky)
    assert st.stored == 1 and st.errors == 0 and calls["n"] == 3
