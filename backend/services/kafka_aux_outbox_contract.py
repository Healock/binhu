"""Metadata-only Kafka contracts for non-task Outbox sources.

These events intentionally use a separate envelope from ``task.*`` events:
photo writeback and venue synchronization are operational intents, not task
domain changes, and must never be made to look like one.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from .kafka_event_contract import EventContractError, MAX_EVENT_BYTES

PHOTO_ACTIONS = frozenset({"mark_completed", "sync_photo", "cancel"})
VENUE_ACTIONS = frozenset({"create", "update", "disable", "delete", "rotate"})


def _uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise EventContractError(f"invalid {field}")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise EventContractError(f"invalid {field}") from None


def _positive(value: Any, field: str) -> int:
    if type(value) is not int or value < 1:
        raise EventContractError(f"invalid {field}")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, datetime):
        raise EventContractError("UTC datetime required")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def photo_outbox_to_event(row: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
    event = {
        "schema_version": 1,
        "event_type": "photo.writeback.requested",
        "event_id": _uuid(row.get("request_id") or row.get("outbox_id"), "event_id"),
        "source_id": _positive(row.get("source_id"), "source_id"),
        "work_order_id": _positive(row.get("work_order_id"), "work_order_id"),
        "action": row.get("action"),
        "timestamp": _timestamp(row.get("timestamp") or row.get("created_at")),
        "environment": "shadow",
        "run_id": run_id,
    }
    if event["action"] not in PHOTO_ACTIONS:
        raise EventContractError("invalid photo action")
    return _validate(event, ("schema_version", "event_type", "event_id", "source_id", "work_order_id", "action", "timestamp", "environment", "run_id"))


def venue_outbox_to_event(row: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
    event = {
        "schema_version": 1,
        "event_type": "venue.sync.requested",
        "event_id": _uuid(row.get("request_id") or row.get("outbox_id"), "event_id"),
        "venue_id": _positive(row.get("venue_id"), "venue_id"),
        "config_revision": _positive(row.get("config_revision"), "config_revision"),
        "action": row.get("action"),
        "timestamp": _timestamp(row.get("timestamp") or row.get("created_at")),
        "environment": "shadow",
        "run_id": run_id,
    }
    if event["action"] not in VENUE_ACTIONS:
        raise EventContractError("invalid venue action")
    return _validate(event, ("schema_version", "event_type", "event_id", "venue_id", "config_revision", "action", "timestamp", "environment", "run_id"))


def _validate(event: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    if set(event) != set(fields) or event["schema_version"] != 1 or event["environment"] != "shadow":
        raise EventContractError("invalid auxiliary event envelope")
    if not isinstance(event["run_id"], str) or not event["run_id"].startswith("KSHADOW-"):
        raise EventContractError("invalid run_id")
    if len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_EVENT_BYTES:
        raise EventContractError("event too large")
    return event


def partition_key(event: Mapping[str, Any]) -> bytes:
    if event.get("event_type") == "photo.writeback.requested":
        return f"photo:{event['source_id']}:{event['work_order_id']}".encode()
    if event.get("event_type") == "venue.sync.requested":
        return f"venue:{event['venue_id']}".encode()
    raise EventContractError("unsupported auxiliary event")
