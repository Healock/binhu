"""Strict metadata-only Kafka contracts for non-task Outbox sources.

These events intentionally use a separate envelope from ``task.*`` events:
photo writeback and venue synchronization are operational intents, not task
domain changes, and must never be made to look like one.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .kafka_event_contract import EventContractError, MAX_EVENT_BYTES, MAX_INT64

SCHEMA_VERSION = 1
PHOTO_EVENT_TYPE = "photo.writeback.requested"
VENUE_EVENT_TYPE = "venue.sync.requested"
PHOTO_ACTIONS = frozenset({"append_request", "mark_completed"})
VENUE_ACTIONS = frozenset({"create", "update", "disable", "delete", "rotate"})

_PHOTO_FIELDS = (
    "schema_version", "event_type", "event_id", "source_id",
    "work_order_id", "action", "timestamp", "environment", "run_id",
)
_VENUE_FIELDS = (
    "schema_version", "event_type", "event_id", "venue_id",
    "config_revision", "action", "timestamp", "environment", "run_id",
)
_EVENT_FIELDS = {
    PHOTO_EVENT_TYPE: _PHOTO_FIELDS,
    VENUE_EVENT_TYPE: _VENUE_FIELDS,
}
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_RUN_ID_RE = re.compile(r"^KSHADOW-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PHOTO_EVENT_ID_TABLE = "photo_sheet_outbox"


def _invalid(field: str) -> EventContractError:
    # Keep messages field-oriented.  Never include supplied values in errors.
    return EventContractError(f"invalid Kafka auxiliary event field: {field}")


def _validate_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise _invalid(field)
    return value


def _validate_int(value: Any, field: str, *, minimum: int) -> int:
    # bool is an int subclass, but is never a contract integer.
    if type(value) is not int or value < minimum or value > MAX_INT64:
        raise _invalid(field)
    return value


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise _invalid("timestamp")
    try:
        # The regex requires a literal Z, so offsets and naive timestamps are
        # rejected before this parse.  Parsing also rejects impossible dates.
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise _invalid("timestamp") from exc
    return value


def _validate_run_id(value: Any) -> str:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        raise _invalid("run_id")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, datetime):
        raise _invalid("timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _row(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid("row")
    return value


def _event_id_for_photo(row: Mapping[str, Any], run_id: str) -> str:
    request_id = row.get("request_id")
    if request_id is not None:
        return _validate_uuid(request_id, "event_id")

    # The deployed table only has an AUTO_INCREMENT bigint.  Namespacing the
    # decimal id with the shadow run and fixed schema prevents treating that
    # bigint as a UUID while keeping historical registration idempotent.
    outbox_id = row.get("outbox_id")
    if outbox_id is None:
        outbox_id = row.get("id")
    outbox_id = _validate_int(outbox_id, "outbox_id", minimum=1)
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{run_id}/{_PHOTO_EVENT_ID_TABLE}/{outbox_id}",
    ))


def photo_outbox_to_event(row: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
    row = _row(row)
    run_id = _validate_run_id(run_id)
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_type": PHOTO_EVENT_TYPE,
        "event_id": _event_id_for_photo(row, run_id),
        "source_id": _validate_int(row.get("source_id"), "source_id", minimum=1),
        "work_order_id": _validate_int(
            row.get("work_order_id"), "work_order_id", minimum=1
        ),
        "action": row.get("action"),
        "timestamp": _timestamp(row.get("timestamp") or row.get("created_at")),
        "environment": "shadow",
        "run_id": run_id,
    }
    return validate_aux_event(event)


def venue_outbox_to_event(row: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
    row = _row(row)
    run_id = _validate_run_id(run_id)
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_type": VENUE_EVENT_TYPE,
        "event_id": _validate_uuid(row.get("request_id"), "event_id"),
        "venue_id": _validate_int(row.get("venue_id"), "venue_id", minimum=1),
        "config_revision": _validate_int(
            row.get("config_revision"), "config_revision", minimum=1
        ),
        "action": row.get("action"),
        "timestamp": _timestamp(row.get("timestamp") or row.get("created_at")),
        "environment": "shadow",
        "run_id": run_id,
    }
    return validate_aux_event(event)


def _validate_common(
    event: Mapping[str, Any], fields: tuple[str, ...], event_type: str
) -> dict[str, Any]:
    allowed = frozenset(fields)
    if any(not isinstance(key, str) or key not in allowed for key in event):
        raise _invalid("properties")
    if any(field not in event for field in fields):
        raise _invalid("required properties")
    if type(event["schema_version"]) is not int or event["schema_version"] != SCHEMA_VERSION:
        raise _invalid("schema_version")
    if event["event_type"] != event_type:
        raise _invalid("event_type")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "event_id": _validate_uuid(event["event_id"], "event_id"),
        "timestamp": _validate_timestamp(event["timestamp"]),
        "environment": event["environment"],
        "run_id": _validate_run_id(event["run_id"]),
    }
    if normalized["environment"] != "shadow":
        raise _invalid("environment")
    return normalized


def validate_aux_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize one persisted auxiliary event envelope."""
    if not isinstance(event, Mapping):
        raise _invalid("event")
    event_type = event.get("event_type")
    if not isinstance(event_type, str):
        raise _invalid("event_type")
    fields = _EVENT_FIELDS.get(event_type)
    if fields is None:
        raise _invalid("event_type")
    normalized = _validate_common(event, fields, event_type)
    normalized["action"] = event["action"]
    if not isinstance(normalized["action"], str):
        raise _invalid("action")
    if event_type == PHOTO_EVENT_TYPE:
        normalized["source_id"] = _validate_int(
            event["source_id"], "source_id", minimum=1
        )
        normalized["work_order_id"] = _validate_int(
            event["work_order_id"], "work_order_id", minimum=1
        )
        if normalized["action"] not in PHOTO_ACTIONS:
            raise _invalid("action")
        # Rebuild in the public field order so callers cannot depend on input
        # mapping order and the serializer remains deterministic.
        return {key: normalized[key] for key in _PHOTO_FIELDS}

    normalized["venue_id"] = _validate_int(event["venue_id"], "venue_id", minimum=1)
    normalized["config_revision"] = _validate_int(
        event["config_revision"], "config_revision", minimum=1
    )
    if normalized["action"] not in VENUE_ACTIONS:
        raise _invalid("action")
    return {key: normalized[key] for key in _VENUE_FIELDS}


def _dump(event: Mapping[str, Any]) -> str:
    return json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def serialize_aux_event(event: Mapping[str, Any]) -> bytes:
    """Return canonical UTF-8 JSON for a validated auxiliary event."""
    payload = _dump(validate_aux_event(event)).encode("utf-8")
    if len(payload) > MAX_EVENT_BYTES:
        raise _invalid("size")
    return payload


def partition_key(event: Mapping[str, Any]) -> bytes:
    normalized = validate_aux_event(event)
    if normalized["event_type"] == PHOTO_EVENT_TYPE:
        return f"photo:{normalized['source_id']}:{normalized['work_order_id']}".encode()
    if normalized["event_type"] == VENUE_EVENT_TYPE:
        return f"venue:{normalized['venue_id']}".encode()
    raise _invalid("event_type")


__all__ = [
    "PHOTO_ACTIONS",
    "VENUE_ACTIONS",
    "EventContractError",
    "partition_key",
    "photo_outbox_to_event",
    "serialize_aux_event",
    "validate_aux_event",
    "venue_outbox_to_event",
]
