"""Metadata projection for police dispatch workflow activity."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any
from .kafka_event_contract import validate_task_event


def project_events(events: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    seen: set[str] = set(); result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in events:
        event = validate_task_event(raw)
        if event["event_id"] in seen: continue
        seen.add(event["event_id"])
        key = (event["run_id"], event["timestamp"][:10])
        row = result.setdefault(key, {"environment": event["environment"], "run_id": event["run_id"],
            "business_date": key[1], "dispatch_events": 0, "assignment_events": 0,
            "review_events": 0, "latest_revision": 0})
        row["latest_revision"] = max(row["latest_revision"], event["revision"])
        suffix = event["event_type"].removeprefix("task.")
        if suffix in {"created", "saved"}: row["dispatch_events"] += 1
        elif suffix == "assigned": row["assignment_events"] += 1
        elif suffix == "reviewed": row["review_events"] += 1
    return result
