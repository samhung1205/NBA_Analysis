"""Reuse verified history partitions across prediction and retrain processes."""
from dataclasses import asdict
from datetime import date, datetime, timedelta

from psycopg.pq import TransactionStatus

from ..models.pregame_features import GameRecord
from ..timeutil import UTC, parse_utc
from .input_cache import load_partitions


def _encode(base):
    games, stages, derived, players = base
    # JSON sorts object keys as strings on read. Normalize before hashing too.
    return {
        "games": [{**asdict(g), "game_time_utc": g.game_time_utc.isoformat()} for g in games],
        "stages": {str(gid): stage for gid, stage in stages.items()},
        "derived": [[gid, tid, values] for (gid, tid), values in derived.items()],
        "players": {str(gid): rows for gid, rows in players.items()},
    }


def _decode(data):
    return (
        [GameRecord(**{**g, "game_time_utc": parse_utc(g["game_time_utc"])}) for g in data["games"]],
        {int(k): v for k, v in data["stages"].items()},
        {(gid, tid): values for gid, tid, values in data["derived"]},
        {int(k): [tuple(r) for r in rows] for k, rows in data["players"].items()},
    )


def load_base(cur, as_of, directory, fetch, history_queries):
    if cur.connection.info.transaction_status != TransactionStatus.IDLE:
        return fetch(cur, as_of)

    def read_partition(bucket):
        start = datetime.combine(date.fromisoformat(bucket), datetime.min.time(), tzinfo=UTC)
        return fetch(cur, min(as_of, start + timedelta(days=7)), since=start)

    games, stages, derived, players = [], {}, {}, {}
    for batch in load_partitions(cur, directory, "boxscores", history_queries(as_of),
                                 read_partition, _encode, _decode):
        batch_games, batch_stages, batch_derived, batch_players = batch
        games.extend(batch_games)
        stages.update(batch_stages)
        derived.update(batch_derived)
        players.update(batch_players)
    return games, stages, derived, players
