"""Cold storage keeps historical input and the live provenance contract."""
from datetime import date, timedelta

import pytest
from psycopg.errors import RaiseException

from core import quota_archive
from core.injury_asof import load_index
from core.jobs.daily import ingest_injury_rows
from core.production.injury_cache import load_recent
from core.timeutil import tpe_date
from test_decision_db import NOW, _ready, account, record_bet, run, user
from test_pricing_artifact import TIP, game, league, setup  # noqa: F401


def _table(cur, name):
    cur.execute(f"SELECT to_jsonb(r) AS row FROM {name} r ORDER BY id")
    return [r["row"] for r in cur.fetchall()]


def test_archive_keeps_live_references_and_round_trips(db, game, setup, tmp_path):
    _ready(db, game, setup)
    uid = user(db)
    account(db, uid)
    run(db, uid=uid)
    day = tpe_date(TIP)
    recent = NOW + timedelta(days=7)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM decision_snapshots WHERE betting_day=%s", (day,))
        referenced_snapshot = cur.fetchone()["id"]
        cur.execute("SELECT * FROM decision_opportunities WHERE snapshot_id=%s AND scope_kind='taiwan_primary' LIMIT 1",
                    (referenced_snapshot,))
        opp = cur.fetchone()
        assert opp is not None
        record_bet(cur, uid, game, stake=5.0, opp=opp)
        # Same day, strictly newer headlines; also add a superseded snapshot
        # without an actual bet and one sizing replacement for every quote.
        for suffix in ("superseded", "latest"):
            cur.execute("""INSERT INTO decision_snapshots
                SELECT (jsonb_populate_record(NULL::decision_snapshots,to_jsonb(s) || jsonb_build_object(
                    'id',nextval('decision_snapshots_id_seq'),'input_fingerprint',%s::text,
                    'last_confirmed_at',last_confirmed_at + interval '1 minute' * %s))).*
                FROM decision_snapshots s WHERE id=%s RETURNING id""",
                        (suffix, 1 if suffix == "superseded" else 2, referenced_snapshot))
            sid = cur.fetchone()["id"]
            cur.execute("""INSERT INTO decision_opportunities
                SELECT (jsonb_populate_record(NULL::decision_opportunities,to_jsonb(o) || jsonb_build_object(
                    'id',nextval('decision_opportunities_id_seq'),'snapshot_id',%s::bigint))).*
                FROM decision_opportunities o WHERE snapshot_id=%s""", (sid, referenced_snapshot))
        cur.execute("""INSERT INTO bet_sizing_snapshots
            SELECT (jsonb_populate_record(NULL::bet_sizing_snapshots,to_jsonb(s) || jsonb_build_object(
                'id',nextval('bet_sizing_snapshots_id_seq'),'portfolio_key','new-head',
                'analysis_as_of',analysis_as_of + interval '1 minute'))).*
            FROM bet_sizing_snapshots s""")
        seed = [{
            "game_date": "10/21/2026", "matchup": "BOS@NYK", "team": "Boston Celtics",
            "player_name": "Synthetic, Player", "status": "Out", "reason": "fixture",
        }]
        ingest_injury_rows(cur, seed, NOW - timedelta(minutes=10))
        ingest_injury_rows(cur, seed, recent)
        cutoffs = dict(injury_before=(NOW+timedelta(days=1)).date(), snapshots_before=day+timedelta(days=1))
        old_history = load_index(cur)
        assert sum(len(entries) for s in old_history.snapshots for entries in s.entries.values()) == 2
        old_cache = load_recent(cur, NOW-timedelta(hours=1), NOW, tmp_path)
        names = ("injury_report_entries", "decision_snapshots", "decision_opportunities", "bet_sizing_snapshots", "bets")
        original = {name: _table(cur, name) for name in names}
        scope = quota_archive.preview(cur, **cutoffs)
        scope.pop("database_bytes")
        cur.execute(quota_archive._OLD_DECISIONS, (cutoffs["snapshots_before"],))
        selected_ids = {r["id"] for r in cur.fetchall()}
    assert referenced_snapshot not in selected_ids
    assert selected_ids == {r["id"] for r in original["decision_snapshots"]
                            if r["input_fingerprint"] == "superseded"}
    assert scope["decisions"] == 1 and scope["opportunities"] > 0
    assert scope["sizing"] > 0 and scope["injuries"] == 1
    with db.get_pool().connection() as conn:
        with pytest.raises(ValueError, match="changed since review"):
            quota_archive.archive_history(conn, **cutoffs, expected_counts={**scope, "injuries": 0})
        assert quota_archive.archive_history(conn, **cutoffs, expected_counts=scope) == scope
    with db.cursor() as cur:
        assert load_index(cur) == old_history
        assert load_recent(cur, NOW-timedelta(hours=1), NOW, tmp_path) == old_cache
        assert _table(cur, "bets") == original["bets"]
        assert {r["input_fingerprint"] for r in _table(cur, "decision_snapshots") if r["betting_day"] == day.isoformat()} >= {"latest"}
        assert any(r["id"] == referenced_snapshot for r in _table(cur, "decision_snapshots"))
        assert any(r["id"] == opp["sizing_snapshot_id"] for r in _table(cur, "bet_sizing_snapshots"))
        with pytest.raises(RaiseException, match="immutable"):
            cur.execute("DELETE FROM decision_snapshots WHERE input_fingerprint='latest'")
        with pytest.raises(RaiseException, match="immutable"):
            cur.execute("DELETE FROM decision_opportunities WHERE snapshot_id=%s", (referenced_snapshot,))
        left = quota_archive.preview(cur, **cutoffs)
        assert left["decisions"] == 0 and left["sizing"] == 0 and left["injuries"] == 0
    with db.get_pool().connection() as conn:
        quota_archive.restore_history(conn)
    with db.cursor() as cur:
        assert {name: _table(cur, name) for name in names} == original
        assert load_index(cur) == old_history
