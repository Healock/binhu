"""Python dual-track sink for the online-source metadata projection."""
from __future__ import annotations

from typing import Any

from .online_source_projection import flatten_projection


TABLE = "dev_online_source_projection_python"


def upsert_sql(row: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    fields = flatten_projection(row, source="python")
    names = (
        "run_id", "task_id", "source_id", "revision", "event_count",
        "address_match_revision", "community_revision", "assignment_revision",
        "task_graph_revision", "watch_snapshot_revision", "last_event_type",
    )
    sql = (
        f"INSERT INTO {TABLE} (" + ",".join(names) + ") VALUES (" + ",".join(["%s"] * len(names)) + ") "
        "ON DUPLICATE KEY UPDATE revision=VALUES(revision),event_count=VALUES(event_count),"
        "address_match_revision=VALUES(address_match_revision),community_revision=VALUES(community_revision),"
        "assignment_revision=VALUES(assignment_revision),task_graph_revision=VALUES(task_graph_revision),"
        "watch_snapshot_revision=VALUES(watch_snapshot_revision),last_event_type=VALUES(last_event_type)"
    )
    return sql, tuple(fields[name] for name in names)


__all__ = ["TABLE", "upsert_sql"]
