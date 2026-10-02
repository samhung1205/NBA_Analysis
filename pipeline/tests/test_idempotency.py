"""重複 ingestion / UPSERT 冪等"""
from datetime import datetime

from core.timeutil import UTC


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def count(db, table, where="TRUE", params=()):
    with db.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}", params)
        return cur.fetchone()["n"]


def test_upsert_game_twice_is_one_row_and_updates_in_place(seeded, db):
    kw = dict(nba_game_id="0022600001", season="2026-27", season_stage="regular", date_utc=utc(2026, 10, 24, 23),
              home_team_id=seeded["BOS"], away_team_id=seeded["NYK"])
    with db.cursor() as cur:
        a = db.upsert_game(cur, status="scheduled", **kw)
        b = db.upsert_game(cur, status="live", home_pts=10, away_pts=8, **kw)
        c = db.upsert_game(cur, status="final", home_pts=100, away_pts=90, **kw)
        assert a == b == c
        cur.execute("SELECT status, home_pts FROM games")
        assert cur.fetchall() == [{"status": "final", "home_pts": 100}]


def test_partial_upsert_does_not_wipe_existing_quarters(seeded, db):
    """schedule 來源沒有逐節資料：只帶部分欄位的 UPSERT 不可把既有的逐節/半場清成 NULL。"""
    kw = dict(nba_game_id="0022600001", season="2026-27", season_stage="regular", date_utc=utc(2026, 10, 24, 23),
              home_team_id=seeded["BOS"], away_team_id=seeded["NYK"], status="final")
    with db.cursor() as cur:
        db.upsert_game(cur, home_q1=25, home_h1=50, **kw)
        db.upsert_game(cur, home_pts=100, **kw)
        cur.execute("SELECT home_q1, home_h1, home_pts FROM games")
        assert cur.fetchone() == {"home_q1": 25, "home_h1": 50, "home_pts": 100}


def test_normalized_game_ingestion_twice(seeded, db):
    from core.jobs.games_sync import _team_maps, upsert_normalized_game

    g = {"nba_game_id": "0022600001", "source": "nba_cdn", "season": "2026-27", "season_stage": "regular",
         "date_utc": utc(2026, 10, 24, 23), "status": "scheduled", "home_nba_team_id": 1610612738,
         "away_nba_team_id": 1610612752, "home_abbr": "BOS", "away_abbr": "NYK", "home_pts": None,
         "away_pts": None}
    with db.cursor() as cur:
        maps = _team_maps(cur)
        ids = {upsert_id for upsert_id in (upsert_normalized_game(cur, g, *maps) for _ in range(3))}
    assert len(ids) == 1 and count(db, "games") == 1


def test_unknown_team_is_skipped_not_fatal(seeded, db):
    from core.jobs.games_sync import _team_maps, upsert_normalized_game

    g = {"nba_game_id": "0022600009", "source": "nba_cdn", "season": "2026-27", "season_stage": "regular",
         "date_utc": utc(2026, 10, 24, 23), "status": "scheduled", "home_nba_team_id": 999,
         "away_nba_team_id": 998, "home_abbr": "XXX", "away_abbr": "YYY", "home_pts": None, "away_pts": None}
    with db.cursor() as cur:
        assert upsert_normalized_game(cur, g, *_team_maps(cur)) is None
    assert count(db, "games") == 0


def test_box_score_ingestion_twice(seeded, db):
    from core.jobs.games_sync import _store_basic, _store_advanced
    from tests.test_fallback import _box, _final_game_in_db

    gid = _final_game_in_db(db, seeded)
    game = {"id": gid, "home_team_id": seeded["BOS"], "away_team_id": seeded["NYK"]}
    adv = {"pace": 98.0, "off_rtg": 110.0}
    with db.cursor() as cur:
        for _ in range(3):
            _store_basic(cur, game, _box())
            _store_advanced(cur, game, {"home": adv, "away": adv})
    assert count(db, "team_game_stats") == 2
    assert count(db, "player_game_stats") == 2
    assert count(db, "players", "nba_player_id IN (1627759, 1629020)") == 2
    with db.cursor() as cur:
        cur.execute("SELECT pace, pts FROM team_game_stats ORDER BY id LIMIT 1")
        assert cur.fetchone() == {"pace": 98.0, "pts": 100}      # 進階數據 UPSERT 不覆蓋基本數據


def test_data_sources_heartbeat_upsert_is_idempotent(db):
    with db.cursor() as cur:
        for _ in range(3):
            db.heartbeat(cur, source_key="espn", display_name="ESPN", category="stats", status="ok",
                         records_updated=5, expected_interval_min=60)
    assert count(db, "data_sources") == 1


def test_injury_duplicate_ingestion_same_snapshot(seeded, db):
    with db.cursor() as cur:
        cur.execute("SELECT id FROM players WHERE name = 'Jaylen Brown'")
        pid = cur.fetchone()["id"]
        res = [db.insert_injury_if_changed(cur, report_time_utc=utc(2026, 1, 15, 22), player_id=pid,
                                           team_id=seeded["BOS"], game_id=None, status="Out", reason="Knee",
                                           source="nba_official") for _ in range(4)]
    assert res == [True, False, False, False] and count(db, "injuries") == 1


def test_injury_same_report_same_player_is_unique_even_with_different_status(seeded, db):
    """C.5B：uq_injuries_report_player — 同一份報告對同一球員最多一列（多賽事日由快照表保存，
    injuries 只放「最近賽事日」的最新狀態）；先寫入者勝，後者被 ON CONFLICT 吸收，重跑穩定。"""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM players WHERE name = 'Jaylen Brown'")
        pid = cur.fetchone()["id"]
        def put(status, reason):
            return db.insert_injury_if_changed(cur, report_time_utc=utc(2026, 1, 15, 22), player_id=pid,
                                               team_id=seeded["BOS"], game_id=None, status=status,
                                               reason=reason, source="nba_official")
        assert [put("Questionable", "Knee"), put("Out", "Rest")] == [True, False]
        assert [put("Questionable", "Knee"), put("Out", "Rest")] == [False, False]
    assert count(db, "injuries") == 1
