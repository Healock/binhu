"""Strict task events with encrypted business snapshots for the isolated pipeline.

This module defines a new Kafka contract.  It deliberately does not reuse the
Redis/outbox ``domain_events`` schema: Kafka consumers receive only stable
task metadata and must read any derived data through the approved readback
boundary.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..identity import environment_for_run_id


SCHEMA_VERSION = 6
LEGACY_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})
MAX_EVENT_BYTES = 256 * 1024
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
        "photo.requested",
        "photo.attached",
        "venue.visited",
        "external.stats.updated",
        "task.view_state.changed",
        "task.view_state.removed",
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
        "photo_attachment",
        "venue_visit",
        "external_stat",
        "online_summary",
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
OPTIONAL_EVENT_FIELDS = ("encrypted_payload", "row_group_token")
V4_EVENT_FIELDS = ("command_id", *EVENT_FIELDS)
V4_REQUIRED_FIELDS = (*V4_EVENT_FIELDS, "encrypted_payload")
V6_EVENT_FIELDS = (*EVENT_FIELDS, "command_id")
V6_REQUIRED_FIELDS = (*V6_EVENT_FIELDS, "row_group_token", "encrypted_payload")
V5_EVENT_FIELDS = (*EVENT_FIELDS, "view_domain", "task_revision")
V5_REQUIRED_FIELDS = (*V5_EVENT_FIELDS, "encrypted_payload")
VIEW_STATE_DOMAINS = frozenset({
    "address_match", "watch", "qmf", "residence", "registration", "review", "task_graph",
})

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


class EventContractError(ValueError):
    """Raised when an event cannot be represented by the Kafka v1 contract."""


def unique_task_events(events):
    """Yield validated events once and reject conflicting duplicate IDs."""
    seen: dict[str, str] = {}
    for raw in events:
        event = validate_task_event(raw)
        signature = json.dumps(event, sort_keys=True, separators=(",", ":"))
        previous = seen.get(event["event_id"])
        if previous is not None:
            if previous != signature:
                raise EventContractError("event id reused with different metadata")
            continue
        seen[event["event_id"]] = signature
        yield event


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
    """Validate and canonicalize one Kafka task event.

    The returned dictionary is a fresh object with no unknown properties and
    deterministic metadata ordering. Any business snapshot is an opaque,
    authenticated encryption envelope.
    """
    if not isinstance(event, Mapping):
        raise _invalid("event")

    if not isinstance(event, Mapping):
        raise _invalid("event")

    version_value = event.get("schema_version")
    if type(version_value) is not int or version_value not in (*LEGACY_SCHEMA_VERSIONS, SCHEMA_VERSION, 5):
        raise _invalid("schema_version")
    contract_fields = (
        V5_EVENT_FIELDS if version_value == 5
        else V6_EVENT_FIELDS if version_value == SCHEMA_VERSION
        else V4_EVENT_FIELDS if version_value == 4
        else EVENT_FIELDS
    )
    allowed = frozenset((*contract_fields, *OPTIONAL_EVENT_FIELDS))
    if any(not isinstance(key, str) or key not in allowed for key in event):
        raise _invalid("properties")
    if any(field not in event for field in contract_fields):
        raise _invalid("required properties")

    schema_version = version_value
    command_id = _validate_uuid(event["command_id"], "command_id") if schema_version in {4, SCHEMA_VERSION} else None
    row_group_token = None

    event_id = _validate_uuid(event["event_id"], "event_id")
    event_type = event["event_type"]
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise _invalid("event_type")
    if schema_version == SCHEMA_VERSION and event_type != "task.deleted":
        row_group_token = event.get("row_group_token")
        if not isinstance(row_group_token, str) or not re.fullmatch(r"[0-9a-f]{64}", row_group_token):
            raise _invalid("row_group_token")
    view_domain = None
    task_revision = None
    if schema_version == 5:
        if event_type not in {"task.view_state.changed", "task.view_state.removed"}:
            raise _invalid("event_type")
        view_domain = event.get("view_domain")
        if view_domain not in VIEW_STATE_DOMAINS:
            raise _invalid("view_domain")
        task_revision = _validate_int(event.get("task_revision"), "task_revision", minimum=1)
    elif event_type in {"task.view_state.changed", "task.view_state.removed"}:
        raise _invalid("schema_version")

    task_id = _validate_task_id(event["task_id"])
    source_id = _validate_int(event["source_id"], "source_id", minimum=1)
    revision = _validate_int(event["revision"], "revision", minimum=0)
    operation_id = _validate_uuid(event["operation_id"], "operation_id")
    changed_fields = _validate_changed_fields(event["changed_fields"])
    timestamp = _validate_timestamp(event["timestamp"])
    encrypted_payload = event.get("encrypted_payload")
    if encrypted_payload is not None:
        expected_payload_fields = {
            "algorithm", "key_version", "nonce", "ciphertext", "aad",
            "ciphertext_sha256",
        }
        if not isinstance(encrypted_payload, Mapping) or set(encrypted_payload) != expected_payload_fields:
            raise _invalid("encrypted_payload")
        if schema_version in {4, SCHEMA_VERSION}:
            expected_key_version = "task-event-v3" if schema_version == SCHEMA_VERSION else "task-event-v2"
            if (encrypted_payload.get("algorithm") != "AES-256-GCM"
                    or encrypted_payload.get("key_version") != expected_key_version):
                raise _invalid("encrypted_payload")
            encoded = encrypted_payload.get("ciphertext")
            nonce = encrypted_payload.get("nonce")
            aad = encrypted_payload.get("aad")
            digest = encrypted_payload.get("ciphertext_sha256")
            if (not isinstance(encoded, str) or not encoded or len(encoded) > MAX_EVENT_BYTES
                    or not isinstance(nonce, str) or len(nonce) != 16
                    or not isinstance(aad, str) or not aad
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise _invalid("encrypted_payload")
            try:
                ciphertext = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
                nonce_bytes = base64.b64decode(nonce + "=" * (-len(nonce) % 4), altchars=b"-_", validate=True)
                aad_bytes = base64.b64decode(aad + "=" * (-len(aad) % 4), altchars=b"-_", validate=True)
            except (ValueError, TypeError):
                raise _invalid("encrypted_payload") from None
            if len(nonce_bytes) != 12 or hashlib.sha256(ciphertext).hexdigest() != digest:
                raise _invalid("encrypted_payload")
            expected_aad = {
                "command_id": command_id,
                "contract": "binhu.task-snapshot.v3" if schema_version == SCHEMA_VERSION else "binhu.task-snapshot.v2",
                "environment": event["environment"],
                "event_id": event_id,
                "revision": revision,
                "task_id": task_id,
            }
            if schema_version == SCHEMA_VERSION:
                expected_aad["row_group_token"] = row_group_token
            if aad_bytes != _dump(expected_aad).encode("utf-8"):
                raise _invalid("encrypted_payload")
        elif schema_version == 5:
            if (encrypted_payload.get("algorithm") != "AES-256-GCM"
                    or encrypted_payload.get("key_version") != "task-view-state-v1"):
                raise _invalid("encrypted_payload")
            encoded = encrypted_payload.get("ciphertext")
            nonce = encrypted_payload.get("nonce")
            aad = encrypted_payload.get("aad")
            digest = encrypted_payload.get("ciphertext_sha256")
            if (not isinstance(encoded, str) or not encoded or len(encoded) > MAX_EVENT_BYTES
                    or not isinstance(nonce, str) or len(nonce) != 16
                    or not isinstance(aad, str) or not aad
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise _invalid("encrypted_payload")
            try:
                ciphertext = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
                nonce_bytes = base64.b64decode(nonce + "=" * (-len(nonce) % 4), altchars=b"-_", validate=True)
                aad_bytes = base64.b64decode(aad + "=" * (-len(aad) % 4), altchars=b"-_", validate=True)
            except (ValueError, TypeError):
                raise _invalid("encrypted_payload") from None
            expected_aad = {
                "contract": "binhu.task-view-state.v1",
                "domain": view_domain,
                "domain_revision": revision,
                "environment": event["environment"],
                "event_id": event_id,
                "run_id": event["run_id"],
                "task_id": task_id,
                "task_revision": task_revision,
            }
            if (len(nonce_bytes) != 12
                    or hashlib.sha256(ciphertext).hexdigest() != digest
                    or aad_bytes != _dump(expected_aad).encode("utf-8")):
                raise _invalid("encrypted_payload")
    if schema_version in {4, SCHEMA_VERSION} and event_type != "task.deleted" and encrypted_payload is None:
        raise _invalid("encrypted_payload")
    if schema_version in {4, SCHEMA_VERSION} and event_type == "task.deleted" and encrypted_payload is not None:
        raise _invalid("encrypted_payload")
    if schema_version == 5 and encrypted_payload is None:
        raise _invalid("encrypted_payload")

    run_id = event["run_id"]
    if not isinstance(run_id, str):
        raise _invalid("run_id")
    try:
        expected_environment = environment_for_run_id(run_id)
    except ValueError:
        raise _invalid("run_id") from None
    environment = event["environment"]
    if not isinstance(environment, str) or environment != expected_environment:
        raise _invalid("environment")

    normalized = {
        "schema_version": schema_version,
        "event_id": event_id,
        "event_type": event_type,
        "task_id": task_id,
        "source_id": source_id,
        "revision": revision,
        "operation_id": operation_id,
        "changed_fields": changed_fields,
        "timestamp": timestamp,
        "environment": expected_environment,
        "run_id": run_id,
    }
    if command_id is not None:
        normalized["command_id"] = command_id
    if row_group_token is not None:
        normalized["row_group_token"] = row_group_token
    if schema_version == 5:
        normalized["view_domain"] = view_domain
        normalized["task_revision"] = task_revision
    if encrypted_payload is not None:
        normalized["encrypted_payload"] = dict(encrypted_payload)
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
    "unique_task_events",
    "task_event_partition_key",
    "validate_event",
    "validate_task_event",
]
