"""Strict conversion of a MySQL domain Outbox row to Kafka metadata.

This pure adapter does not enqueue, acknowledge or modify the existing SSE Outbox.
Missing historical task mappings remain rejected for explicit migration review.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .kafka_event_contract import EventContractError, validate_task_event

# Existing SELECT * order, including the four additive metadata columns.
_COLUMNS = (
    "event_id", "schema_version", "domain", "event_type", "aggregate_type",
    "aggregate_id", "aggregate_revision", "audiences_json", "status",
    "attempt_count", "available_at", "locked_by", "locked_until",
    "last_error_code", "last_error_summary", "occurred_at", "published_at",
    "task_id", "source_id", "operation_id", "changed_fields_json",
)


def domain_row_to_task_event(row, *, run_id, environment="shadow"):
    """Convert explicit local metadata; never infer identity from legacy keys."""
    if isinstance(row, dict):
        source = row
    elif isinstance(row, (tuple, list)) and len(row) == len(_COLUMNS):
        source = dict(zip(_COLUMNS, row))
    else:
        raise EventContractError("task source mapping required")
    if environment != "shadow":
        raise EventContractError("shadow identity required")
    occurred = source.get("occurred_at")
    if not isinstance(occurred, datetime):
        raise EventContractError("Outbox UTC datetime required")
    # MySQL DATETIME is read in a connection configured to UTC.
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    fields = source.get("changed_fields_json")
    try:
        if isinstance(fields, (str, bytes, bytearray)):
            fields = json.loads(fields)
    except (ValueError, TypeError):
        raise EventContractError("invalid changed fields") from None
    event = {
        "schema_version": 1,
        "event_id": source.get("event_id"),
        "event_type": source.get("event_type"),
        "task_id": source.get("task_id"),
        "source_id": source.get("source_id"),
        "revision": source.get("aggregate_revision"),
        "operation_id": source.get("operation_id"),
        "changed_fields": fields,
        "timestamp": occurred.astimezone(timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
        "environment": environment,
        "run_id": run_id,
    }
    # Validate raw types before any coercion: True must never become revision 1.
    return validate_task_event(event)
