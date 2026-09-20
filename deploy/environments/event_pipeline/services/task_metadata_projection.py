"""Deterministic task metadata projection used by the Dev dual-track check.

The projection intentionally contains no business正文.  It aggregates the
metadata-only Kafka contract into counters that can be rebuilt from Kafka and
compared with the legacy Python worker.  Production and Staging never import
this module.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Any, Iterable

from .kafka_event_contract import validate_task_event
from ..identity import environment_for_run_id


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
        "environment": environment_for_run_id(run_id),
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


class IncrementalTaskMetadataProjector:
    """Apply one validated event without retaining the complete event stream.

    The event-id cache is bounded so a long-running Dev worker cannot grow
    without limit.  The default retains more IDs than the 100,000-event gate;
    the durable output table remains the source of truth after a restart.
    """

    def __init__(self, max_event_ids: int = 250_000):
        if max_event_ids < 1:
            raise ValueError("max_event_ids must be positive")
        self.max_event_ids = max_event_ids
        self._seen: dict[str, str] = {}
        self._order: deque[str] = deque()
        self._rows: dict[tuple[str, str, int], dict[str, Any]] = {}

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        event = validate_task_event(raw)
        canonical = _canonical(event)
        previous = self._seen.get(event["event_id"])
        if previous is not None:
            if previous != canonical:
                raise ProjectionConflict("event id reused with different metadata")
            return self._rows[_key(event)]
        self._seen[event["event_id"]] = canonical
        self._order.append(event["event_id"])
        while len(self._order) > self.max_event_ids:
            self._seen.pop(self._order.popleft(), None)

        key = _key(event)
        row = self._rows.setdefault(key, _empty(key))
        # A task revision is immutable metadata.  Conflicting replay must be
        # visible to the acceptance ledger instead of silently overwriting it.
        revision_signature = _revision_signature(event)
        revisions = row.setdefault("_revision_signatures", {})
        old_signature = revisions.get(event["revision"])
        if old_signature is not None and old_signature != revision_signature:
            raise ProjectionConflict("revision has incompatible metadata")
        revisions[event["revision"]] = revision_signature
        row["revision"] = max(row["revision"], event["revision"])
        row["event_count"] += 1
        row["changed_field_count"] += len(event["changed_fields"])
        event_name = event["event_type"].removeprefix("task.")
        row["event_counts"][event_name] += 1
        return row

    def restore_snapshot(self, value: dict[str, Any]) -> None:
        """Restore one aggregate from the durable Python projection table.

        Kafka offsets are a delivery cursor, not a projection checkpoint.  A
        worker restart must therefore hydrate its reducer before consuming new
        records; otherwise the next event would overwrite a partial in-memory
        count with a lower one.
        """
        required = {
            "environment", "run_id", "task_id", "source_id", "revision",
            "event_count", "changed_field_count", *[f"{name}_count" for name in EVENT_COUNTERS],
        }
        if set(value) != required:
            raise ValueError("invalid persisted projection identity")
        key = (value["run_id"], value["task_id"], value["source_id"])
        try:
            environment = environment_for_run_id(value["run_id"])
        except ValueError:
            raise ValueError("invalid persisted projection identity") from None
        if value.get("environment") != environment:
            raise ValueError("invalid persisted projection identity")
        if not isinstance(value["run_id"], str) or not isinstance(value["task_id"], str):
            raise ValueError("invalid persisted projection key")
        if type(value["source_id"]) is not int or value["source_id"] <= 0:
            raise ValueError("invalid persisted projection source")
        numeric = ["revision", "event_count", "changed_field_count", *[f"{name}_count" for name in EVENT_COUNTERS]]
        if any(type(value[name]) is not int or value[name] < 0 for name in numeric):
            raise ValueError("invalid persisted projection counters")
        self._rows[key] = {
            "environment": environment, "run_id": key[0], "task_id": key[1],
            "source_id": key[2], "revision": value["revision"],
            "event_count": value["event_count"],
            "changed_field_count": value["changed_field_count"],
            "event_counts": {name: value[f"{name}_count"] for name in EVENT_COUNTERS},
        }

    def snapshot(self, key: tuple[str, str, int]) -> dict[str, Any]:
        row = self._rows[key].copy()
        row.pop("_revision_signatures", None)
        row["event_counts"] = dict(row["event_counts"])
        return row

    @property
    def event_cache_size(self) -> int:
        return len(self._seen)


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


__all__ = [
    "EVENT_COUNTERS", "IncrementalTaskMetadataProjector", "ProjectionConflict",
    "flatten_projection", "project_events",
]
