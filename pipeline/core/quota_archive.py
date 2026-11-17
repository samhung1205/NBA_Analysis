"""Compact historical data without losing source rows or active references.

Call archive_history only in a dedicated maintenance transaction after approval
and a verified backup. PostgreSQL commits each archive and removal together.
"""
from datetime import date
from uuid import uuid4


_OLD_DECISIONS = """
    WITH ranked AS (
        SELECT id, betting_day,
               row_number() OVER (PARTITION BY user_id, betting_day ORDER BY last_confirmed_at DESC, id DESC) AS rank
        FROM decision_snapshots
    ), referenced AS (
        SELECT decision_snapshot_id AS id FROM bets WHERE decision_snapshot_id IS NOT NULL
        UNION
        SELECT o.snapshot_id FROM bets b JOIN decision_opportunities o ON o.id = b.decision_opportunity_id
    )
    SELECT r.id FROM ranked r WHERE r.betting_day < %s AND r.rank > 1
      AND NOT EXISTS (SELECT 1 FROM referenced p WHERE p.id = r.id)
"""

_OLD_SIZING = """
    WITH heads AS (
        SELECT market_pricing_snapshot_id, risk_policy_version, max(analysis_as_of) AS latest
        FROM bet_sizing_snapshots GROUP BY 1, 2
    ), day_heads AS (
        SELECT betting_day, risk_policy_version, max(analysis_as_of) AS latest
        FROM bet_sizing_snapshots GROUP BY 1, 2
    ), referenced AS (
        SELECT sizing_snapshot_id AS id FROM decision_opportunities WHERE sizing_snapshot_id IS NOT NULL
        UNION
        SELECT reference_sizing_snapshot_id FROM bets WHERE reference_sizing_snapshot_id IS NOT NULL
    )
    SELECT s.id FROM bet_sizing_snapshots s
    JOIN heads h USING (market_pricing_snapshot_id, risk_policy_version)
    JOIN day_heads d USING (betting_day, risk_policy_version)
    WHERE s.betting_day < %s AND s.analysis_as_of < h.latest AND s.analysis_as_of < d.latest
      AND NOT EXISTS (SELECT 1 FROM referenced p WHERE p.id = s.id)
"""

_OLD_INJURIES = """
    SELECT e.id FROM injury_reports r JOIN injury_report_entries e ON e.report_id = r.id
    WHERE r.report_time_utc < (%s::date::timestamp AT TIME ZONE 'UTC')
"""


def _cutoffs(cur, injury_before, snapshots_before):
    if not isinstance(injury_before, date) or not isinstance(snapshots_before, date):
        raise TypeError("explicit date cutoffs are required")
    cur.execute("""SELECT (now() AT TIME ZONE 'Asia/Taipei')::date - 3 AS local_before,
                          (now() AT TIME ZONE 'UTC')::date - 7 AS utc_before""")
    bounds = cur.fetchone()
    if injury_before > bounds["utc_before"] or snapshots_before > bounds["local_before"]:
        raise ValueError("retain at least seven UTC days of injury entries and three Taipei days of snapshots")


def preview(cur, *, injury_before: date, snapshots_before: date):
    """Count eligible rows without changing database state."""
    _cutoffs(cur, injury_before, snapshots_before)
    cur.execute(
        f"""WITH injury_ids AS ({_OLD_INJURIES}), decision_ids AS ({_OLD_DECISIONS}), sizing_ids AS ({_OLD_SIZING})
        SELECT (SELECT count(*) FROM injury_ids) AS injuries,
               (SELECT count(*) FROM decision_ids) AS decisions,
               (SELECT count(*) FROM decision_opportunities o JOIN decision_ids d ON d.id = o.snapshot_id) AS opportunities,
               (SELECT count(*) FROM sizing_ids) AS sizing,
               pg_database_size(current_database()) AS database_bytes""",
        (injury_before, snapshots_before, snapshots_before),
    )
    return cur.fetchone()


def _pack_sizing(cur):
    # Keep a stable batch size so a single TOAST value is always bounded.
    cur.execute("""WITH moved AS (
        DELETE FROM bet_sizing_snapshots s USING quota_sizing_ids ids WHERE s.id = ids.id RETURNING s.*
    ), batches AS (
        SELECT id / 250 AS bucket, min(betting_day) AS oldest, max(betting_day) AS newest,
               count(*) AS n, jsonb_agg(to_jsonb(moved) ORDER BY id) AS payload
        FROM moved GROUP BY 1
    ), packed AS (
        INSERT INTO cold_sizing_batches (batch_key, oldest_day, newest_day, row_count, entries)
        SELECT %s || ':' || bucket, oldest, newest, n, payload FROM batches RETURNING row_count
    ) SELECT COALESCE(sum(row_count),0)::bigint AS moved FROM packed""", (uuid4().hex,))
    return cur.fetchone()["moved"]


def _pack_injuries(cur):
    cur.execute("""WITH moved AS (
        DELETE FROM injury_report_entries e USING quota_injury_ids ids WHERE e.id = ids.id RETURNING e.*
    ), packed AS (
        INSERT INTO cold_injury_entries (report_id,row_count,entries)
        SELECT report_id,count(*),jsonb_agg(to_jsonb(moved) ORDER BY id) FROM moved GROUP BY report_id
        RETURNING row_count
    ) SELECT COALESCE(sum(row_count),0)::bigint AS moved FROM packed""")
    return cur.fetchone()["moved"]


def _pack_decisions(cur):
    cur.execute("""INSERT INTO cold_decisions (snapshot_id,snapshot,opportunities)
        SELECT d.id,to_jsonb(d),COALESCE(o.entries,'[]'::jsonb)
        FROM decision_snapshots d JOIN quota_decision_ids ids ON ids.id = d.id
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id) AS entries FROM decision_opportunities o WHERE o.snapshot_id=d.id
        ) o ON true""")
    packed = cur.rowcount
    # Both tables are locked for the full transaction. Preserve the normal
    # immutability guards when the transaction ends, including on rollback.
    cur.execute("ALTER TABLE decision_opportunities DISABLE TRIGGER trg_decision_opp_immutable")
    cur.execute("ALTER TABLE decision_snapshots DISABLE TRIGGER trg_decision_snapshot_guard")
    cur.execute("DELETE FROM decision_opportunities o USING quota_decision_ids ids WHERE o.snapshot_id=ids.id")
    opportunities = cur.rowcount
    cur.execute("DELETE FROM decision_snapshots d USING quota_decision_ids ids WHERE d.id=ids.id")
    if cur.rowcount != packed:
        raise RuntimeError("decision snapshot copy and removal counts differ")
    cur.execute("ALTER TABLE decision_snapshots ENABLE TRIGGER trg_decision_snapshot_guard")
    cur.execute("ALTER TABLE decision_opportunities ENABLE TRIGGER trg_decision_opp_immutable")
    return packed, opportunities


def archive_history(conn, *, injury_before: date, snapshots_before: date, expected_counts: dict):
    """Move exactly the pre-reviewed scope or roll back the operation."""
    from psycopg.pq import TransactionStatus

    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("a dedicated idle connection is required")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '180s'")
        cur.execute("""LOCK TABLE bets, decision_snapshots, decision_opportunities, bet_sizing_snapshots,
                       injury_reports, injury_report_entries IN SHARE ROW EXCLUSIVE MODE""")
        _cutoffs(cur, injury_before, snapshots_before)
        for table, select, cutoff in (
            ("quota_injury_ids", _OLD_INJURIES, injury_before),
            ("quota_decision_ids", _OLD_DECISIONS, snapshots_before),
            ("quota_sizing_ids", _OLD_SIZING, snapshots_before),
        ):
            cur.execute(f"CREATE TEMP TABLE {table} ON COMMIT DROP AS {select}", (cutoff,))
            cur.execute(f"CREATE UNIQUE INDEX ON {table}(id)")
        cur.execute("""SELECT (SELECT count(*) FROM quota_injury_ids) AS injuries,
                       (SELECT count(*) FROM quota_decision_ids) AS decisions,
                       (SELECT count(*) FROM decision_opportunities o JOIN quota_decision_ids ids ON ids.id=o.snapshot_id) AS opportunities,
                       (SELECT count(*) FROM quota_sizing_ids) AS sizing""")
        scope = cur.fetchone()
        if dict(scope) != expected_counts:
            raise ValueError("archive candidates changed since review")
        injuries = _pack_injuries(cur) if scope["injuries"] else 0
        sizing = _pack_sizing(cur) if scope["sizing"] else 0
        decisions, opportunities = _pack_decisions(cur) if scope["decisions"] else (0, 0)
        moved = dict(injuries=injuries, sizing=sizing, decisions=decisions, opportunities=opportunities)
        if moved != dict(scope):
            raise RuntimeError("archive row counts differ; rolling back")
        return moved


def restore_history(conn):
    """Restore all cold rows for an explicitly approved rollback.

    Identity and reference conflicts fail the transaction. Original sequence
    values stay ahead because archiving never resets a sequence.
    """
    from psycopg.pq import TransactionStatus

    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("a dedicated idle connection is required")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '180s'")
        cur.execute("""LOCK TABLE decision_snapshots, decision_opportunities, bet_sizing_snapshots,
                       injury_report_entries, cold_injury_entries, cold_decisions,
                       cold_sizing_batches IN SHARE ROW EXCLUSIVE MODE""")
        for live, cold, payload, single_row in (
            ("injury_report_entries", "cold_injury_entries", "entries", False),
            ("bet_sizing_snapshots", "cold_sizing_batches", "entries", False),
            ("decision_snapshots", "cold_decisions", "snapshot", True),
            ("decision_opportunities", "cold_decisions", "opportunities", False),
        ):
            fn = "jsonb_populate_record" if single_row else "jsonb_populate_recordset"
            cur.execute(f"""INSERT INTO {live} SELECT src.*
                FROM {cold} cold CROSS JOIN LATERAL {fn}(NULL::{live}, cold.{payload}) src""")
        for cold in ("cold_injury_entries", "cold_decisions", "cold_sizing_batches"):
            cur.execute(f"DELETE FROM {cold}")
