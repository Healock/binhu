"""Photo request/attachment metadata projection; binary data never enters Kafka."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any
from .kafka_event_contract import validate_task_event


def project_events(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    seen: set[str] = set(); result: dict[str, dict[str, Any]] = {}
    for raw in events:
        event = validate_task_event(raw)
        if event["event_id"] in seen: continue
        seen.add(event["event_id"])
        row = result.setdefault(event["run_id"], {"environment": event["environment"], "run_id": event["run_id"],
            "requested": 0, "attached": 0, "latest_revision": 0})
        row["latest_revision"] = max(row["latest_revision"], event["revision"])
        if event["event_type"] == "photo.requested": row["requested"] += 1
        if event["event_type"] == "photo.attached": row["attached"] += 1
    return result
