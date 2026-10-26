"""Metadata-only daily ledger projection for the event pipeline."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any
from .kafka_event_contract import validate_task_event


def project_events(events: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    seen: set[str] = set()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in events:
        event = validate_task_event(raw)
        if event["event_id"] in seen:
            continue
        seen.add(event["event_id"])
        key = (event["run_id"], event["timestamp"][:10])
        row = result.setdefault(key, {"environment": event["environment"], "run_id": event["run_id"],
            "business_date": key[1], "event_count": 0, "created_count": 0,
            "saved_count": 0, "completed_count": 0, "deleted_count": 0,
            "latest_revision": 0})
        row["event_count"] += 1
        row["latest_revision"] = max(row["latest_revision"], event["revision"])
        suffix = event["event_type"].removeprefix("task.")
        if suffix == "created": row["created_count"] += 1
        elif suffix == "saved": row["saved_count"] += 1
        elif suffix == "reviewed": row["completed_count"] += 1
        elif suffix == "deleted": row["deleted_count"] += 1
    return result


__all__ = ["project_events"]
