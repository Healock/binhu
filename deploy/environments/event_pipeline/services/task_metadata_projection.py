"""Deterministic task metadata projection used by the Dev dual-track check.

The projection intentionally contains no business正文.  It aggregates the
metadata-only Kafka contract into counters that can be rebuilt from Kafka and
compared with the legacy Python worker.  Production and Staging never import
this module.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from .kafka_event_contract import validate_task_event


EVENT_COUNTERS = (
    "created",
    "saved",
    "claimed",
    "assigned",
    "reviewed",
    "archived",
    "deleted",
)


class ProjectionConflict(ValueError):
    """Two events claim the same revision with incompatible metadata."""


def _key(event: dict[str, Any]) -> tuple[str, str, int]:
    return event["run_id"], event["task_id"], event["source_id"]


def _empty(key: tuple[str, str, int]) -> dict[str, Any]:
    run_id, task_id, source_id = key
    return {
        "environment": "development",
        "run_id": run_id,
        "task_id": task_id,
        "source_id": source_id,
        "revision": 0,
        "event_count": 0,
        "changed_field_count": 0,
        "event_counts": {name: 0 for name in EVENT_COUNTERS},
    }


def project_events(events: Iterable[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Reduce validated metadata events into per-task counters.

    Event IDs are deduplicated.  A duplicate ID with different content is a
    hard conflict; silently choosing one would make the Python and Flink
    tracks appear converged while hiding a broken delivery contract.
    """
    seen: dict[str, str] = {}
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for raw in events:
        event = validate_task_event(raw)
        canonical = _canonical(event)
        previous = seen.get(event["event_id"])
        if previous is not None:
            if previous != canonical:
                raise ProjectionConflict("event id reused with different metadata")
            continue
        seen[event["event_id"]] = canonical
        grouped[_key(event)].append(event)

    result: dict[tuple[str, str, int], dict[str, Any]] = {}
    for key, items in grouped.items():
        revisions: dict[int, str] = {}
        projection = _empty(key)
        counts = Counter()
        changed = 0
        for event in items:
            revision = event["revision"]
            signature = _revision_signature(event)
            if revision in revisions and revisions[revision] != signature:
                raise ProjectionConflict("revision has incompatible metadata")
            revisions[revision] = signature
            counts[event["event_type"].removeprefix("task.")] += 1
            changed += len(event["changed_fields"])
        projection["revision"] = max(revisions, default=0)
        projection["event_count"] = len(items)
        projection["changed_field_count"] = changed
        projection["event_counts"] = {name: counts[name] for name in EVENT_COUNTERS}
        result[key] = projection
    return result


def _revision_signature(event: dict[str, Any]) -> str:
    return f"{event['event_type']}|{','.join(event['changed_fields'])}"


def _canonical(event: dict[str, Any]) -> str:
    fields = ",".join(event["changed_fields"])
    return "|".join(
        (
            event["event_id"], event["event_type"], event["task_id"],
            str(event["source_id"]), str(event["revision"]), fields,
            event["operation_id"], event["timestamp"], event["environment"],
            event["run_id"],
        )
    )


def flatten_projection(value: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, JSON-serializable comparison shape."""
    result = {key: value[key] for key in (
        "environment", "run_id", "task_id", "source_id", "revision",
        "event_count", "changed_field_count",
    )}
    result.update({f"{name}_count": value["event_counts"][name] for name in EVENT_COUNTERS})
    return result


__all__ = ["EVENT_COUNTERS", "ProjectionConflict", "flatten_projection", "project_events"]
