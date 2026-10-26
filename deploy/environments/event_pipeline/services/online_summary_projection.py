"""Rebuildable metadata counters for online summary refresh activity."""
from __future__ import annotations

from typing import Any
from collections.abc import Iterable

from .kafka_event_contract import unique_task_events


def project_events(events: Iterable[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in unique_task_events(events):
        key = (event["run_id"], event["timestamp"][:10], event["event_type"])
        row = result.setdefault(key, {"environment": event["environment"], "run_id": event["run_id"],
            "business_date": key[1], "event_type": key[2], "event_count": 0,
            "task_count": set(), "changed_field_count": 0})
        row["event_count"] += 1
        row["task_count"].add((event["task_id"], event["source_id"]))
        row["changed_field_count"] += len(event["changed_fields"])
    for row in result.values():
        row["task_count"] = len(row["task_count"])
    return result


def flatten_projection(row: dict[str, Any], *, source: str) -> dict[str, Any]:
    return {"environment": row["environment"], "run_id": row["run_id"],
            "business_date": row["business_date"], "event_type": row["event_type"],
            "event_count": row["event_count"], "task_count": row["task_count"],
            "changed_field_count": row["changed_field_count"], "source": source}


__all__ = ["flatten_projection", "project_events"]
