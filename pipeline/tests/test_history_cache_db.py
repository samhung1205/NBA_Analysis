"""Content-verified cache vs direct as-of loader, in the random-schema fixture."""
from datetime import datetime, timedelta
import json

from core import metrics
from core.production import inputs
from core.timeutil import UTC

NOW = datetime(2026, 1, 15, 22, 7, tzinfo=UTC)


def _seed(db, seeded, when=NOW-timedelta(days=1), nba="cache-history"):
    with db.cursor() as cur:
        gid = db.upsert_game(cur, nba_game_id=nba, season="2025-26", season_stage="regular",
                             date_utc=when, home_team_id=seeded["BOS"], away_team_id=seeded["NYK"],
                             status="final", home_pts=110, away_pts=100, home_h1=55, away_h1=50)
        cur.execute("SELECT id FROM players ORDER BY id LIMIT 1")
        pid = cur.fetchone()["id"]
        db.upsert_player_game_stats(cur, game_id=gid, player_id=pid, team_id=seeded["BOS"],
                                    min=30.0, started=1, plus_minus=10.0)
        db.upsert_team_derived(cur, game_id=gid, team_id=seeded["BOS"], formula_version=metrics.FORMULA_VERSION,
                              **{k: 1.0 for k in inputs.TEAM_METRICS})
    return gid, pid


def _read(db, at=NOW, directory=None):
    with db.cursor() as cur:
        return inputs.load_history(cur, at, cache_dir=directory)


def test_unchanged_content_transfers_no_historical_rows(db, seeded, tmp_path, monkeypatch):
    # Fixture IDs 2 and 10 reproduce v1's numeric versus JSON string sort order.
    with db.cursor() as cur:
        cur.execute("ALTER SEQUENCE games_id_seq RESTART WITH 2")
    _seed(db, seeded)
    with db.cursor() as cur:
        cur.execute("ALTER SEQUENCE games_id_seq RESTART WITH 10")
    _seed(db, seeded, nba="second-cache-game")
    reference = _read(db)
    fetch, calls = inputs._load_history_base, []
    def counted(*a, **kw):
        calls.append(1)
        return fetch(*a, **kw)
    monkeypatch.setattr(inputs, "_load_history_base", counted)
    assert _read(db, directory=tmp_path) == reference
    assert _read(db, directory=tmp_path) == reference
    assert calls == [1]


def test_new_boxscore_only_downloads_its_week(db, seeded, tmp_path, monkeypatch):
    _seed(db, seeded, NOW - timedelta(days=21), "old-week")
    _seed(db, seeded, NOW - timedelta(days=1), "current-week")
    assert _read(db, directory=tmp_path) == _read(db)
    fetch = inputs._load_history_base
    _seed(db, seeded, NOW - timedelta(hours=3), "new-final")
    expected = _read(db)
    starts = []

    def counted(*args, since):
        starts.append(since)
        return fetch(*args, since=since)

    monkeypatch.setattr(inputs, "_load_history_base", counted)
    assert _read(db, directory=tmp_path) == expected
    assert starts == [datetime(2026, 1, 12, tzinfo=UTC)]


def test_corrections_and_deletions_invalidate_without_new_ids(db, seeded, tmp_path):
    gid, pid = _seed(db, seeded)
    first = _read(db, directory=tmp_path)
    with db.cursor() as cur:
        cur.execute("UPDATE player_game_stats SET min=12.5 WHERE game_id=%s AND player_id=%s", (gid, pid))
        cur.execute("UPDATE team_game_derived SET pace=99 WHERE game_id=%s", (gid,))
        cur.execute("UPDATE games SET home_pts=111 WHERE id=%s", (gid,))
    changed = _read(db, directory=tmp_path)
    assert changed == _read(db) and changed != first
    with db.cursor() as cur:
        cur.execute("DELETE FROM player_game_stats WHERE game_id=%s", (gid,))
    assert _read(db, directory=tmp_path) == _read(db)
    assert gid not in _read(db, directory=tmp_path).players


def test_future_history_and_backwards_asof_never_reuse_future_inputs(db, seeded, tmp_path):
    _seed(db, seeded)
    gid, _ = _seed(db, seeded, NOW+timedelta(hours=1), "future-final-fixture")
    past = _read(db, directory=tmp_path)
    assert gid not in {g.game_id for g in past.games}
    future = _read(db, NOW+timedelta(hours=2), tmp_path)
    assert gid in {g.game_id for g in future.games}
    assert _read(db, directory=tmp_path) == past == _read(db)


def test_corrupt_cache_falls_back_to_complete_inputs(db, seeded, tmp_path):
    _seed(db, seeded)
    ref = _read(db, directory=tmp_path)
    path, = tmp_path.glob('*.json')
    value = json.loads(path.read_text())
    value['body']['games'] = []   # valid JSON but checksum no longer matches
    path.write_text(json.dumps(value))
    assert _read(db, directory=tmp_path) == ref
    path.write_text('{broken')
    assert _read(db, directory=tmp_path) == ref


def test_unwritable_cache_keeps_fresh_inputs(db, seeded, tmp_path):
    _seed(db, seeded)
    path = tmp_path/'file-not-directory'
    path.write_text('x')
    assert _read(db, directory=path) == _read(db)


def test_caller_transaction_is_not_committed_or_reconfigured(db, seeded, tmp_path):
    _seed(db, seeded)
    with db.transaction() as cur:
        cur.execute("SELECT transaction_timestamp() AS t")
        before = cur.fetchone()['t']
        inputs.load_history(cur, NOW, cache_dir=tmp_path)
        cur.execute("SELECT transaction_timestamp() AS t")
        assert cur.fetchone()['t'] == before
    assert list(tmp_path.glob('*.json')) == []


def test_injuries_are_loaded_fresh_on_base_cache_hit(db, seeded, tmp_path, monkeypatch):
    _seed(db, seeded)
    assert not _read(db, directory=tmp_path).injury_index.snapshots
    from core.jobs.daily import ingest_injury_rows
    with db.cursor() as cur:
        ingest_injury_rows(cur, [{'game_date':'01/15/2026','matchup':'BOS@NYK','team':'Boston Celtics',
            'player_name':'Brown, Jaylen','status':'Out','reason':'Synthetic fixture'}], NOW-timedelta(minutes=7))
    def must_not_reload(*a, **kw):
        raise AssertionError('unchanged base was downloaded again')
    monkeypatch.setattr(inputs, '_load_history_base', must_not_reload)
    got = _read(db, directory=tmp_path)
    assert len(got.injury_index.snapshots) == 1


def test_recent_injuries_only_reload_changed_day_and_respect_cutoff(db, seeded, tmp_path, monkeypatch):
    from core.jobs.daily import ingest_injury_rows
    from core.production import injury_cache

    row = {'game_date': '01/15/2026', 'matchup': 'BOS@NYK', 'team': 'Boston Celtics',
           'player_name': 'Brown, Jaylen', 'status': 'Out', 'reason': 'Synthetic fixture'}
    with db.cursor() as cur:
        ingest_injury_rows(cur, [row], NOW - timedelta(days=1))
        ingest_injury_rows(cur, [row], NOW - timedelta(minutes=7))
        ingest_injury_rows(cur, [row], NOW)
    assert _read(db, directory=tmp_path) == _read(db)
    assert len(_read(db, directory=tmp_path).injury_index.snapshots) == 2
    with db.cursor() as cur:
        cur.execute("""UPDATE injury_report_entries SET status='Available'
            WHERE report_id=(SELECT id FROM injury_reports WHERE report_time_utc=%s)""",
                    (NOW - timedelta(minutes=7),))
    expected = _read(db)
    fetch = injury_cache.load_index
    starts = []

    def counted(cur, *, since, until):
        starts.append(since)
        return fetch(cur, since=since, until=until)

    monkeypatch.setattr(injury_cache, "load_index", counted)
    assert _read(db, directory=tmp_path) == expected
    assert _read(db, directory=tmp_path) == expected
    assert starts == [datetime(2026, 1, 15, tzinfo=UTC)]
