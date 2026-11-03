"""Verified on-disk reuse of unchanged historical box-score inputs.

Every call checks the actual as-of data content in Postgres. Counts/MAX(id)/TTL
are insufficient: corrected scores, deleted rows and late stats must invalidate
the cache too. Fingerprint and cold read share one read-only repeatable snapshot.
Injury snapshots are deliberately loaded fresh by inputs.load_history.
"""
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import tempfile

from psycopg.pq import TransactionStatus

from .. import metrics
from ..models.pregame_features import GameRecord, TEAM_METRICS
from ..timeutil import parse_utc
from .features import HISTORY_STAGES

log = logging.getLogger(__name__)
VERSION = 1


def revision(cur, as_of):
    # Hash only columns used by _load_history_base, with the same predicates.
    # Aggregation stays inside Postgres; only three digests cross the network.
    cur.execute("""WITH game_rows AS (
        SELECT g.id,g.season,g.season_stage,g.date_utc,g.home_team_id,g.away_team_id,
               ht.abbr AS h_abbr,at.abbr AS a_abbr,g.home_pts,g.away_pts,g.home_h1,g.away_h1
        FROM games g JOIN teams ht ON ht.id=g.home_team_id JOIN teams at ON at.id=g.away_team_id
        WHERE g.status='final' AND g.season_stage=ANY(%s)
          AND g.home_pts IS NOT NULL AND g.away_pts IS NOT NULL AND g.date_utc<%s
    ), derived_rows AS (
        SELECT d.game_id,d.team_id,""" + ",".join(f"d.{m}" for m in TEAM_METRICS) + """
        FROM team_game_derived d JOIN games g ON g.id=d.game_id
        WHERE d.formula_version=%s AND g.status='final' AND g.date_utc<%s
    ), player_rows AS (
        SELECT s.game_id,s.team_id,s.player_id,s.min,s.started,s.plus_minus
        FROM player_game_stats s JOIN games g ON g.id=s.game_id
        WHERE g.status='final' AND g.date_utc<%s
    ) SELECT current_database() AS db,current_schema() AS schema,
        (SELECT md5(COALESCE(string_agg(md5(r::text),'' ORDER BY r.id),'')) FROM game_rows r) AS games,
        (SELECT md5(COALESCE(string_agg(md5(r::text),'' ORDER BY r.game_id,r.team_id),'')) FROM derived_rows r) AS derived,
        (SELECT md5(COALESCE(string_agg(md5(r::text),'' ORDER BY r.game_id,r.team_id,r.player_id),'')) FROM player_rows r) AS players
    """, (list(HISTORY_STAGES), as_of, metrics.FORMULA_VERSION, as_of, as_of))
    return cur.fetchone()


def _encode(base):
    games, stages, derived, players = base
    return {"games": [{**asdict(g), "game_time_utc": g.game_time_utc.isoformat()} for g in games],
            "stages": stages, "derived": [[gid, tid, values] for (gid, tid), values in derived.items()],
            "players": players}


def _decode(data):
    return ([GameRecord(**{**g, "game_time_utc": parse_utc(g["game_time_utc"])}) for g in data["games"]],
            {int(k): v for k, v in data["stages"].items()},
            {(gid, tid): values for gid, tid, values in data["derived"]},
            {int(k): [tuple(r) for r in rows] for k, rows in data["players"].items()})


def _digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def load_base(cur, as_of, directory, fetch):
    # Do not change isolation or commit a caller-owned transaction. Such callers
    # use the reference loader; live prediction cursors are normally autocommit.
    if cur.connection.info.transaction_status != TransactionStatus.IDLE:
        return fetch(cur, as_of)
    with cur.connection.transaction():
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rev = revision(cur, as_of)
        info = cur.connection.info
        scope = _digest([info.host, info.port, info.user, rev["db"], rev["schema"]])
        path = Path(directory) / f"{scope}.json"
        try:
            cached = json.loads(path.read_text())
            if (cached["version"] == VERSION and cached["revision"] == rev
                    and cached["sha256"] == _digest(cached["base"])):
                base = _decode(cached["base"])
                log.info("[history-cache] hit; historical rows not transferred")
                return base
        except (OSError, ValueError, TypeError, KeyError):
            pass
        base = fetch(cur, as_of)
    # Persist only a coherent cold read, atomically. A cache failure never turns
    # into empty/stale model inputs; the fresh reference data is returned.
    temp_path = None
    try:
        data = _encode(base)
        payload = {"version": VERSION, "revision": rev, "base": data, "sha256": _digest(data)}
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            temp_path = Path(f.name)
            json.dump(payload, f, allow_nan=False, separators=(",", ":"))
        temp_path.replace(path)
    except (OSError, ValueError, TypeError):
        log.warning("[history-cache] cache write unavailable; using fresh DB inputs")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    log.info("[history-cache] miss; historical input cache refreshed")
    return base
