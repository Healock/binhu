from __future__ import annotations
from typing import Any

TABLE = "dev_daily_ledger_projection_python"


def upsert_sql(row: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    names = ("run_id", "business_date", "event_count", "created_count", "saved_count", "completed_count", "deleted_count", "latest_revision")
    sql = (f"INSERT INTO {TABLE} (" + ",".join(names) + ") VALUES (" + ",".join(["%s"] * len(names)) + ") "
           "ON DUPLICATE KEY UPDATE event_count=VALUES(event_count),created_count=VALUES(created_count),"
           "saved_count=VALUES(saved_count),completed_count=VALUES(completed_count),deleted_count=VALUES(deleted_count),"
           "latest_revision=VALUES(latest_revision)")
    return sql, tuple(row[name] for name in names)
