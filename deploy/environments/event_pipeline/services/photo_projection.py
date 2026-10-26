"""Photo request/attachment metadata projection; binary data never enters Kafka."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any
from .kafka_event_contract import unique_task_events


def project_events(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for event in unique_task_events(events):
        row = result.setdefault(event["run_id"], {"environment": event["environment"], "run_id": event["run_id"],
            "requested": 0, "attached": 0, "latest_revision": 0})
        row["latest_revision"] = max(row["latest_revision"], event["revision"])
        if event["event_type"] == "photo.requested": row["requested"] += 1
        if event["event_type"] == "photo.attached": row["attached"] += 1
    return result
