"""Metadata projection for visit and personnel attendance activity."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any
from .kafka_event_contract import validate_task_event


def project_events(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    seen: set[str] = set(); output: dict[str, dict[str, Any]] = {}
    for raw in events:
        event = validate_task_event(raw)
        if event["event_id"] in seen: continue
        seen.add(event["event_id"])
        row = output.setdefault(event["run_id"], {"environment": event["environment"], "run_id": event["run_id"],
            "attendance_events": 0, "review_events": 0, "assignment_events": 0, "latest_revision": 0})
        row["latest_revision"] = max(row["latest_revision"], event["revision"])
        if "inspector" in event["changed_fields"]: row["attendance_events"] += 1
        if event["event_type"] == "task.reviewed": row["review_events"] += 1
        if event["event_type"] == "task.assigned": row["assignment_events"] += 1
    return output
