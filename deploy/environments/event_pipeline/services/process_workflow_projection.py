"""Workflow/process engine metadata projection."""
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
            "claimed": 0, "assigned": 0, "reviewed": 0, "saved": 0, "latest_revision": 0})
        row["latest_revision"] = max(row["latest_revision"], event["revision"])
        name = event["event_type"].removeprefix("task.")
        if name in {"claimed", "assigned", "reviewed", "saved"}: row[name] += 1
    return result
