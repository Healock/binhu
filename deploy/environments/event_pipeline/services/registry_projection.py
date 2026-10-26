"""Safe registry/household metadata projection; never copies person values."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any
from .kafka_event_contract import validate_task_event


def project_events(events: Iterable[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    seen: set[str] = set(); result: dict[tuple[str, str, int], dict[str, Any]] = {}
    for raw in events:
        event = validate_task_event(raw)
        if event["event_id"] in seen: continue
        seen.add(event["event_id"])
        key = (event["run_id"], event["task_id"], event["source_id"])
        row = result.setdefault(key, {"environment": event["environment"], "run_id": event["run_id"],
            "task_id": event["task_id"], "source_id": event["source_id"], "revision": 0,
            "registry_revision": 0, "household_revision": 0, "tag_revision": 0})
        row["revision"] = max(row["revision"], event["revision"])
        if "community" in event["changed_fields"]: row["registry_revision"] = max(row["registry_revision"], event["revision"])
        if "person_tags" in event["changed_fields"]: row["tag_revision"] = max(row["tag_revision"], event["revision"])
        if event["event_type"] in {"task.created", "task.deleted"}: row["household_revision"] = max(row["household_revision"], event["revision"])
    return result
