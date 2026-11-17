"""Content checks and atomic disk storage for prediction inputs."""
import hashlib
import json
import logging
from pathlib import Path
import tempfile

log = logging.getLogger(__name__)
VERSION = 2


def _digest(data):
    body = json.dumps(data, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _read(path, digest, decode):
    try:
        cached = json.loads(path.read_text())
        if (cached["version"] == VERSION and cached["digest"] == digest
                and cached["sha256"] == _digest(cached["body"])):
            return decode(cached["body"])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def _write(path, digest, body):
    temp_path = None
    try:
        payload = {"version": VERSION, "digest": digest, "body": body, "sha256": _digest(body)}
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            temp_path = Path(f.name)
            json.dump(payload, f, allow_nan=False, separators=(",", ":"))
        temp_path.replace(path)
    except (OSError, ValueError, TypeError):
        log.warning("[input-cache] cache write unavailable; using fresh inputs")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_partitions(cur, directory, name, queries, fetch, encode, decode):
    """Yield data in UTC bucket order from one read-only consistent snapshot.

    queries provides SELECTs of (bucket, sort_id, ...data columns); only their
    fingerprints cross the wire on a cache hit. Each changed bucket replaces
    its previous cached version. A removed bucket cannot appear in the result.
    """
    with cur.connection.transaction():
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cur.execute("SELECT current_database() AS db, current_schema() AS schema")
        identity = cur.fetchone()
        info = cur.connection.info
        scope = _digest([info.host, info.port, info.user, identity["db"], identity["schema"]])
        fingerprints = []
        params = []
        for sql, args in queries:
            fingerprints.append(
                "SELECT r.bucket, md5(string_agg(md5(r::text), '' ORDER BY r.sort_id)) AS digest "
                f"FROM ({sql}) r GROUP BY r.bucket"
            )
            params.extend(args)
        cur.execute(
            "SELECT bucket::text, md5(string_agg(digest, '' ORDER BY digest)) AS digest FROM ("
            + " UNION ALL ".join(fingerprints) + ") grouped GROUP BY bucket ORDER BY bucket",
            params,
        )
        hits = misses = 0
        for row in cur.fetchall():
            bucket, digest = row["bucket"], row["digest"]
            path = Path(directory) / f"{scope}-{name}-{bucket}.json"
            value = _read(path, digest, decode)
            if value is None:
                value = fetch(bucket)
                _write(path, digest, encode(value))
                misses += 1
            else:
                hits += 1
            yield value
        log.info("[input-cache] %s hits=%d downloads=%d", name, hits, misses)
