"""Bounded realtime notification metadata projection."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any
from .kafka_event_contract import validate_task_event


def project_events(events: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    seen: set[str] = set()
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in events:
        event = validate_task_event(raw)
        if event["event_id"] in seen:
            continue
        seen.add(event["event_id"])
        key = (event["run_id"], event["event_type"])
        row = output.setdefault(key, {"environment": event["environment"], "run_id": event["run_id"],
            "event_type": event["event_type"], "event_count": 0, "latest_revision": 0,
            "changed_field_count": 0})
        row["event_count"] += 1
        row["latest_revision"] = max(row["latest_revision"], event["revision"])
        row["changed_field_count"] += len(event["changed_fields"])
    return output


__all__ = ["project_events"]
