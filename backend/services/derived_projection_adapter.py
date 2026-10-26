"""Read adapter for rebuildable event-pipeline projections.

The adapter is opt-in for the isolated Dev/Staging derived database.  The
authoritative OnlineData tables and all business writes remain on MySQL.
"""
from __future__ import annotations

import os
from typing import Any


_TABLES = {
    "python": "dev_online_source_projection_python",
    "flink": "dev_online_source_projection",
}


def selected_engine(environment: str, *, environ: dict[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    engine = values.get("ONLINE_SOURCE_PROJECTION_ENGINE", "mysql").strip().lower()
    if engine == "mysql":
        return "mysql"
    if environment not in {"development", "staging"}:
        raise ValueError("event-pipeline projection is unavailable outside Dev/Staging")
    if engine not in _TABLES:
        raise ValueError("unsupported online-source projection engine")
    return engine


async def read_derived_projection(
    cur,
    *,
    environment: str,
    run_id: str,
    task_id: str,
    source_id: int,
    engine: str | None = None,
) -> dict[str, Any] | None:
    """Read a safe derived row; return ``None`` when the projection lags."""
    selected = engine or selected_engine(environment)
    if selected == "mysql":
        raise ValueError("MySQL authoritative read requires the OnlineData repository")
    table = _TABLES[selected]
    await cur.execute(
        f"SELECT run_id,task_id,source_id,revision,event_count,"
        f"address_match_revision,community_revision,assignment_revision,"
        f"task_graph_revision,watch_snapshot_revision,last_event_type "
        f"FROM {table} WHERE run_id=%s AND task_id=%s AND source_id=%s",
        (run_id, task_id, source_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {
        "run_id": row[0], "task_id": row[1], "source_id": int(row[2]),
        "revision": int(row[3]), "event_count": int(row[4]),
        "address_match_revision": int(row[5]),
        "community_revision": int(row[6]),
        "assignment_revision": int(row[7]),
        "task_graph_revision": int(row[8]),
        "watch_snapshot_revision": int(row[9]),
        "last_event_type": row[10], "engine": selected,
    }


__all__ = ["read_derived_projection", "selected_engine"]
