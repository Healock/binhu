"""Python sink contract for realtime event metadata."""
from __future__ import annotations
from typing import Any

TABLE = "dev_realtime_event_projection_python"


def upsert_sql(row: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    names = ("run_id", "event_type", "event_count", "latest_revision", "changed_field_count")
    sql = (f"INSERT INTO {TABLE} (" + ",".join(names) + ") VALUES (" + ",".join(["%s"] * len(names)) + ") "
           "ON DUPLICATE KEY UPDATE event_count=VALUES(event_count),latest_revision=VALUES(latest_revision),"
           "changed_field_count=VALUES(changed_field_count)")
    return sql, tuple(row[name] for name in names)


__all__ = ["TABLE", "upsert_sql"]
