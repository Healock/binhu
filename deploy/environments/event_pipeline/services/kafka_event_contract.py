"""Strict metadata-only events for the isolated Kafka Dev POC.

This module defines a new Kafka contract.  It deliberately does not reuse the
Redis/outbox ``domain_events`` schema: Kafka consumers receive only stable
task metadata and must read any derived data through the approved readback
boundary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 16 * 1024
MAX_INT64 = 2**63 - 1

EVENT_TYPES = frozenset(
    {
        "task.saved",
        "task.claimed",
        "task.assigned",
        "task.reviewed",
        "task.archived",
        "task.created",
        "task.deleted",
    }
)

# These names are the public, English metadata summary.  They describe which
# projection or task facet changed; they never carry the changed value.
CHANGED_FIELDS = frozenset(
    {
        "address",
        "community",
        "inspector",
        "check_result",
        "task_state",
        "small_community",
        "person_tags",
        "task_graph",
        "daily_report",
    }
)

EVENT_FIELDS = (
    "schema_version",
    "event_id",
    "event_type",
    "task_id",
    "source_id",
    "revision",
    "operation_id",
    "changed_fields",
    "timestamp",
    "environment",
    "run_id",
)

# A local source reference is generated from one of these registered parser
# tables and a positive local row id.  Keep this contract allowlist fixed: a
# newly registered parser must explicitly update this public Kafka contract and
# its schema instead of changing accepted messages as a side effect of import
# order or deployment contents.
SOURCE_TABLE_NAMES = frozenset(
    {
        "t_fullchain",
        "t_rental_check",
        "t_police_stats",
        "t_suspect_unrevoked",
        "t_suspect_return",
        "t_delivery_industry",
        "t_group_rental",
        "t_suzhou_police",
        "t_traffic_police",
    }
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SOURCE_REF_RE = re.compile(r"^(?P<table>t_[a-z0-9_]+):(?P<source>[1-9][0-9]*)$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_RUN_ID_RE = re.compile(r"^dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class EventContractError(ValueError):
    """Raised when an event cannot be represented by the Kafka v1 contract."""


def _invalid(field: str) -> EventContractError:
    # Keep messages bounded and field-oriented.  Never include event values,
    # which could accidentally turn a validation error into a data channel.
    return EventContractError(f"invalid Kafka task event field: {field}")


def _validate_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise _invalid(field)
    return value


def _validate_int(value: Any, field: str, *, minimum: int) -> int:
    # bool is an int subclass, but is never a contract integer.
    if type(value) is not int or value < minimum or value > MAX_INT64:
        raise _invalid(field)
    return value


def _validate_task_id(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid("task_id")
    match = _SOURCE_REF_RE.fullmatch(value)
    if match is None or match.group("table") not in SOURCE_TABLE_NAMES:
        raise _invalid("task_id")
    # Check the decimal spelling before converting it.  This keeps hostile,
    # arbitrarily long task IDs from reaching int() and guarantees the error
    # path never leaks the supplied value.
    source_text = match.group("source")
    if len(source_text) > 19 or (
        len(source_text) == 19 and source_text > str(MAX_INT64)
    ):
        raise _invalid("task_id")
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


def _validate_changed_fields(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise _invalid("changed_fields")
    fields: set[str] = set()
    for field in value:
        if not isinstance(field, str) or field not in CHANGED_FIELDS:
            raise _invalid("changed_fields")
        if field in fields:
            raise _invalid("changed_fields")
        fields.add(field)
    # Sorted output makes equivalent input order produce byte-for-byte
    # identical JSON. Duplicates are rejected so Python and JSON Schema have
    # the same strict array semantics.
    return sorted(fields)


def validate_task_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize one metadata-only Kafka task event.

    The returned dictionary is a fresh object.  It has no unknown properties,
    no task body, and a deterministic ``changed_fields`` order.  Kafka
    serialization should use :func:`serialize_task_event`.
    """
    if not isinstance(event, Mapping):
        raise _invalid("event")

    allowed = frozenset(EVENT_FIELDS)
    if any(not isinstance(key, str) or key not in allowed for key in event):
        raise _invalid("properties")
    if any(field not in event for field in EVENT_FIELDS):
        raise _invalid("required properties")

    schema_version = event["schema_version"]
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise _invalid("schema_version")

    event_id = _validate_uuid(event["event_id"], "event_id")
    event_type = event["event_type"]
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise _invalid("event_type")

    task_id = _validate_task_id(event["task_id"])
    source_id = _validate_int(event["source_id"], "source_id", minimum=1)
    revision = _validate_int(event["revision"], "revision", minimum=0)
    operation_id = _validate_uuid(event["operation_id"], "operation_id")
    changed_fields = _validate_changed_fields(event["changed_fields"])
    timestamp = _validate_timestamp(event["timestamp"])

    environment = event["environment"]
    if not isinstance(environment, str) or environment != "development":
        raise _invalid("environment")
    run_id = event["run_id"]
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise _invalid("run_id")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "task_id": task_id,
        "source_id": source_id,
        "revision": revision,
        "operation_id": operation_id,
        "changed_fields": changed_fields,
        "timestamp": timestamp,
        "environment": "development",
        "run_id": run_id,
    }
    if len(_dump(normalized).encode("utf-8")) > MAX_EVENT_BYTES:
        raise _invalid("size")
    return normalized


def _dump(event: Mapping[str, Any]) -> str:
    return json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json(event: Mapping[str, Any]) -> str:
    """Return deterministic UTF-8 JSON text for a validated task event."""
    return _dump(validate_task_event(event))


def serialize_task_event(event: Mapping[str, Any]) -> bytes:
    """Return the canonical UTF-8 Kafka value, bounded to 16 KiB."""
    payload = canonical_json(event).encode("utf-8")
    # Keep the size check adjacent to the transport boundary even though the
    # validator also checks it, so future serializers cannot bypass the limit.
    if len(payload) > MAX_EVENT_BYTES:
        raise _invalid("size")
    return payload


def partition_key(event_or_task_id: Mapping[str, Any] | str, source_id: Any = None) -> bytes:
    """Return the stable Kafka key bytes for a ``task_id`` + ``source_id`` pair.

    The key uses an explicit separator because ``task_id`` itself contains a
    colon.  Passing an event validates the complete contract; passing the two
    components is useful for producer code that has not built the envelope.
    """
    if isinstance(event_or_task_id, Mapping):
        if source_id is not None:
            raise _invalid("partition_key")
        normalized = validate_task_event(event_or_task_id)
        task_id = normalized["task_id"]
        source = normalized["source_id"]
    else:
        task_id = _validate_task_id(event_or_task_id)
        source = _validate_int(source_id, "source_id", minimum=1)
    return f"{task_id}|{source}".encode("utf-8")


def partition_key_text(event_or_task_id: Mapping[str, Any] | str, source_id: Any = None) -> str:
    """Return the stable key as text for logs and deterministic assertions."""
    return partition_key(event_or_task_id, source_id).decode("utf-8")


def task_event_partition_key(event: Mapping[str, Any]) -> bytes:
    """Named alias for callers that want to emphasize the event contract."""
    return partition_key(event)


def partition_key_bytes(event: Mapping[str, Any]) -> bytes:
    """Return the stable partition key in Kafka's usual byte form."""
    return partition_key(event)


def validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Relay-facing alias for :func:`validate_task_event`."""
    return validate_task_event(event)


def encode_event(event: Mapping[str, Any]) -> bytes:
    """Relay-facing alias for :func:`serialize_task_event`."""
    return serialize_task_event(event)


__all__ = [
    "CHANGED_FIELDS",
    "EVENT_FIELDS",
    "EVENT_TYPES",
    "EventContractError",
    "MAX_EVENT_BYTES",
    "SCHEMA_VERSION",
    "SOURCE_TABLE_NAMES",
    "canonical_json",
    "encode_event",
    "partition_key",
    "partition_key_bytes",
    "partition_key_text",
    "serialize_task_event",
    "task_event_partition_key",
    "validate_event",
    "validate_task_event",
]
