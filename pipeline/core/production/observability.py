"""Python materializes public aggregates; API / JS never recalculate model metrics."""
import os

from .monitoring import evaluate_snapshot, fetch_snapshot
from .shadow import dumps, schema_ready, summary


def materialize():
    from ..db import cursor, transaction, heartbeat
    with cursor() as cur:
        if not schema_ready(cur):
            return {"status": "WAITING_SCHEMA"}
    snapshot = fetch_snapshot(14)
    model = evaluate_snapshot(snapshot)
    for evaluation in model["evaluations"].values():
        evaluation.pop("by_artifact", None)
        evaluation.pop("missing_game_ids", None)
    with transaction() as cur:
        cur.execute("SELECT clock_timestamp() AS now")
        now = cur.fetchone()["now"]
        shadow = summary(cur, now)
        for key, value in (("prospective-model-v1", model), ("adaptive-shadow-v1", shadow)):
            cur.execute("""INSERT INTO model_evaluation_reports VALUES (%s,%s,%s)
                ON CONFLICT (report_key) DO UPDATE SET generated_at=EXCLUDED.generated_at,payload=EXCLUDED.payload
                WHERE model_evaluation_reports.generated_at < EXCLUDED.generated_at""", (key, snapshot["checked_at"], dumps(value)))
        heartbeat(cur, source_key="model_monitor", display_name="模型前瞻績效", category="model",
                  expected_interval_min=30, status="ok", records_updated=model["n_final_games"])
    return {"status": "ok", "n_final_games": model["n_final_games"], "shadow": shadow["status"]}


def scheduled():
    if os.getenv("MODEL_MONITOR_ENABLED", "true").lower() not in ("1", "true"):
        return {"status": "DISABLED"}
    return materialize()
