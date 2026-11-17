"""Reuse small result sets only after Postgres verifies their current content."""
from collections import OrderedDict
from copy import deepcopy
from threading import Lock

_ROWS = OrderedDict()
_LOCK = Lock()
_MAX_ENTRIES = 16
_MAX_ROWS_PER_ENTRY = 2500


def read_unchanged(cur, key, select, params, *, order_by="id"):
    """Query and validate the result in one SQL statement and one snapshot.

    The SELECT must return a unique non-null id. An empty result still carries
    a revision so deleted rows cannot survive in the cache.
    """
    with _LOCK:
        previous = _ROWS.get(key)
    revision = previous[0] if previous else None
    cur.execute(
        f"""WITH picked AS MATERIALIZED ({select}),
            version AS (SELECT md5(COALESCE(string_agg(md5(p::text), '' ORDER BY p.id), ''))
                              AS result_revision FROM picked p)
            SELECT version.result_revision, p.*
            FROM version LEFT JOIN picked p ON version.result_revision IS DISTINCT FROM %s
            ORDER BY {order_by}""",
        (*params, revision),
    )
    fetched = cur.fetchall()
    current = fetched[0]["result_revision"]
    if previous is not None and current == revision:
        with _LOCK:
            if key in _ROWS:
                _ROWS.move_to_end(key)
        return deepcopy(previous[1])
    rows = [{k: v for k, v in row.items() if k != "result_revision"} for row in fetched if row["id"] is not None]
    with _LOCK:
        if len(rows) <= _MAX_ROWS_PER_ENTRY:
            _ROWS[key] = (current, deepcopy(rows))
            _ROWS.move_to_end(key)
            if len(_ROWS) > _MAX_ENTRIES:
                _ROWS.popitem(last=False)
        else:
            _ROWS.pop(key, None)
    return rows
