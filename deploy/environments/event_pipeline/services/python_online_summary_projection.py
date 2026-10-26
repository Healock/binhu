"""Python dual-track sink for online summary metadata counters."""
from __future__ import annotations
from typing import Any
from .online_summary_projection import flatten_projection

TABLE = "dev_online_summary_projection_python"


def upsert_sql(row: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    value = flatten_projection(row, source="python")
    names = ("run_id", "business_date", "event_type", "event_count", "task_count", "changed_field_count")
    sql = (f"INSERT INTO {TABLE} (" + ",".join(names) + ") VALUES (" + ",".join(["%s"] * len(names)) + ") "
           "ON DUPLICATE KEY UPDATE event_count=VALUES(event_count),task_count=VALUES(task_count),"
           "changed_field_count=VALUES(changed_field_count)")
    return sql, tuple(value[name] for name in names)


__all__ = ["TABLE", "upsert_sql"]
