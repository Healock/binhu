"""Verify the Staging Python projection matches the Flink projection.

The command emits aggregate counts only.  Task identifiers and business data
never leave the private Staging network or enter deployment logs.
"""
from __future__ import annotations

import asyncio
import json

from .runtime import configuration, connect, pipeline_run_id


async def verify() -> dict[str, object]:
    config = configuration()
    if config["APP_ENVIRONMENT"] != "staging":
        raise ValueError("Staging rollback verification required")
    run_id = pipeline_run_id(config)
    pool = await connect(config)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM dev_task_metadata WHERE run_id=%s),"
                    "(SELECT COUNT(*) FROM dev_task_metadata_python WHERE run_id=%s),"
                    "(SELECT COUNT(*) FROM dev_task_metadata f LEFT JOIN dev_task_metadata_python p "
                    " ON p.run_id=f.run_id AND p.task_id=f.task_id AND p.source_id=f.source_id "
                    " WHERE f.run_id=%s AND (p.task_id IS NULL OR p.revision<>f.revision "
                    " OR p.event_count<>f.event_count OR p.changed_field_count<>f.changed_field_count)),"
                    "(SELECT COUNT(*) FROM dev_task_metadata_python p LEFT JOIN dev_task_metadata f "
                    " ON f.run_id=p.run_id AND f.task_id=p.task_id AND f.source_id=p.source_id "
                    " WHERE p.run_id=%s AND f.task_id IS NULL)",
                    (run_id, run_id, run_id, run_id),
                )
                flink_rows, python_rows, changed, python_only = await cursor.fetchone()
        mismatches = int(changed) + int(python_only)
        return {
            "environment": "staging", "run_id": run_id,
            "flink_projection_rows": int(flink_rows),
            "python_projection_rows": int(python_rows),
            "projection_mismatch_count": mismatches,
            "consistent": mismatches == 0 and int(flink_rows) == int(python_rows),
        }
    finally:
        pool.close()
        await pool.wait_closed()


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(verify()), sort_keys=True))
    except Exception:
        raise SystemExit("Staging rollback consistency verification failed") from None
