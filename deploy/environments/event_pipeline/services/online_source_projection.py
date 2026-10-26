"""Rebuildable metadata projection for the online-source domain.

The online source table remains the MySQL business authority.  This reducer
only carries stable identifiers, revisions and change flags so Flink can
rebuild queue/watch/task-graph read hints without receiving source values.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .kafka_event_contract import validate_task_event


_DIRTY_FIELDS = {
    "address": "address_match",
    "community": "community",
    "inspector": "assignment",
    "task_graph": "task_graph",
    "person_tags": "watch_snapshot",
}


class OnlineSourceProjectionConflict(ValueError):
    """Raised when an event id or revision is reused with different metadata."""


def _key(event: dict[str, Any]) -> tuple[str, str, int]:
    return event["run_id"], event["task_id"], event["source_id"]


def _signature(event: dict[str, Any]) -> str:
    return "|".join((
        event["event_id"], event["event_type"], event["task_id"],
        str(event["source_id"]), str(event["revision"]),
        ",".join(event["changed_fields"]), event["operation_id"],
        event["timestamp"], event["environment"], event["run_id"],
    ))


def _empty(key: tuple[str, str, int]) -> dict[str, Any]:
    run_id, task_id, source_id = key
    return {
        "environment": "development" if run_id.startswith("dev-") else "staging",
        "run_id": run_id,
        "task_id": task_id,
        "source_id": source_id,
        "revision": 0,
        "event_count": 0,
        "address_match_revision": 0,
        "community_revision": 0,
        "assignment_revision": 0,
        "task_graph_revision": 0,
        "watch_snapshot_revision": 0,
        "last_event_type": "",
    }


def apply_event(row: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Apply one validated event to a projection row without source values."""
    revision = event["revision"]
    row["revision"] = max(row["revision"], revision)
    row["event_count"] += 1
    row["last_event_type"] = event["event_type"]
    for field in event["changed_fields"]:
        target = _DIRTY_FIELDS.get(field)
        if target:
            row[f"{target}_revision"] = max(row[f"{target}_revision"], revision)
    return row


def project_events(events: Iterable[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Reduce metadata events into deterministic online-source read hints."""
    seen: dict[str, str] = {}
    revisions: dict[tuple[str, str, int], dict[int, str]] = defaultdict(dict)
    result: dict[tuple[str, str, int], dict[str, Any]] = {}
    for raw in events:
        event = validate_task_event(raw)
        signature = _signature(event)
        previous = seen.get(event["event_id"])
        if previous is not None:
            if previous != signature:
                raise OnlineSourceProjectionConflict("event id reused with different metadata")
            continue
        seen[event["event_id"]] = signature
        key = _key(event)
        revision_signature = "|".join((event["event_type"], ",".join(event["changed_fields"])))
        old = revisions[key].get(event["revision"])
        if old is not None and old != revision_signature:
            raise OnlineSourceProjectionConflict("revision has incompatible metadata")
        revisions[key][event["revision"]] = revision_signature
        result[key] = apply_event(result.setdefault(key, _empty(key)), event)
    return result


def flatten_projection(row: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Return the stable, non-sensitive sink shape."""
    return {
        "environment": row["environment"], "run_id": row["run_id"],
        "task_id": row["task_id"], "source_id": row["source_id"],
        "revision": row["revision"], "event_count": row["event_count"],
        "address_match_revision": row["address_match_revision"],
        "community_revision": row["community_revision"],
        "assignment_revision": row["assignment_revision"],
        "task_graph_revision": row["task_graph_revision"],
        "watch_snapshot_revision": row["watch_snapshot_revision"],
        "last_event_type": row["last_event_type"], "source": source,
    }


__all__ = [
    "OnlineSourceProjectionConflict", "apply_event", "flatten_projection",
    "project_events",
]
