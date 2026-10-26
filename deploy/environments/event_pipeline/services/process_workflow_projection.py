"""Workflow/process engine metadata projection."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any
from .kafka_event_contract import unique_task_events


def project_events(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for event in unique_task_events(events):
        row = result.setdefault(event["run_id"], {"environment": event["environment"], "run_id": event["run_id"],
            "claimed": 0, "assigned": 0, "reviewed": 0, "saved": 0, "latest_revision": 0})
        row["latest_revision"] = max(row["latest_revision"], event["revision"])
        name = event["event_type"].removeprefix("task.")
        if name in {"claimed", "assigned", "reviewed", "saved"}: row[name] += 1
    return result
