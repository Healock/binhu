"""QMF acquisition/registration metadata projection."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any
from .kafka_event_contract import unique_task_events


def project_events(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for event in unique_task_events(events):
        row = output.setdefault(event["run_id"], {"environment": event["environment"], "run_id": event["run_id"],
            "acquisition_events": 0, "registration_events": 0, "latest_revision": 0})
        row["latest_revision"] = max(row["latest_revision"], event["revision"])
        if event["event_type"] in {"task.created", "task.saved"}: row["acquisition_events"] += 1
        if "community" in event["changed_fields"] or event["event_type"] == "task.assigned": row["registration_events"] += 1
    return output
