"""Verify recent injury reports by UTC day and reuse unchanged days."""
from dataclasses import asdict
from datetime import date, datetime, timedelta

from psycopg.pq import TransactionStatus

from ..injury_asof import InjuryIndex, PlayerState, Snapshot, index_queries, load_index
from ..timeutil import UTC, ensure_utc, parse_utc
from .input_cache import load_partitions


def _encode(index):
    return [
        {"id": snap.id, "time": snap.report_time_utc.isoformat(), "coverage": snap.coverage,
         "entries": [[day, abbr, [asdict(player) for player in players]]
                     for (day, abbr), players in snap.entries.items()]}
        for snap in index.snapshots
    ]


def _decode(data):
    return InjuryIndex([
        Snapshot(row["id"], parse_utc(row["time"]), row["coverage"],
                 {(day, abbr): [PlayerState(**p) for p in players]
                  for day, abbr, players in row["entries"]})
        for row in data
    ])


def load_recent(cur, since, as_of, directory):
    if cur.connection.info.transaction_status != TransactionStatus.IDLE:
        return load_index(cur, since=since, until=as_of)
    since = ensure_utc(since)
    # Read the first day as a whole so the rolling lower bound doesn't cause
    # repeat downloads. Enforce the exact lower bound on the returned index.
    first_day = datetime.combine(since.date(), datetime.min.time(), tzinfo=UTC)

    def fetch(bucket):
        start = datetime.combine(date.fromisoformat(bucket), datetime.min.time(), tzinfo=UTC)
        return load_index(cur, since=start, until=min(as_of, start + timedelta(days=1)))

    partitions = load_partitions(cur, directory, "injuries", index_queries(first_day, as_of, fingerprints=True),
                                 fetch, _encode, _decode)
    return InjuryIndex([snap for index in partitions for snap in index.snapshots if snap.report_time_utc >= since])
