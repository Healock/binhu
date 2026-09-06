"""Prepare strict Kafka task metadata alongside the domain event outbox.

The bridge is deliberately opt-in.  It only accepts a shadow ``KSHADOW`` run
and delegates the actual inserts to :func:`domain_events.enqueue_event`, so
the Kafka delivery intent and the business event use the caller's transaction.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from config import Settings
from .domain_events import enqueue_event
from .kafka_event_contract import validate_task_event


_RUN_ID_RE = re.compile(r"^KSHADOW-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


_EVENT_TYPE_MAP = {
    "online.task.created": "task.created",
    "online.task.changed": "task.saved",
    "online.task.deleted": "task.deleted",
    "online.task.claimed": "task.claimed",
    "online.task.assigned": "task.assigned",
    "online.task.reviewed": "task.reviewed",
    "online.task.archived": "task.archived",
}

_FIELD_MAP = {
    "地址": "address",
    "社区": "community",
    "核查人": "inspector",
    "核查结果": "check_result",
    "任务状态": "task_state",
    "小区": "small_community",
    "人员标签": "person_tags",
    "任务图": "task_graph",
    "日报": "daily_report",
    # Callers that already use the public metadata names remain supported.
    "address": "address",
    "community": "community",
    "inspector": "inspector",
    "check_result": "check_result",
    "task_state": "task_state",
    "small_community": "small_community",
    "person_tags": "person_tags",
    "task_graph": "task_graph",
    "daily_report": "daily_report",
}


def bridge_config(settings: Settings) -> dict[str, str] | None:
    """Return the validated shadow bridge identity, or ``None`` when off."""
    if not getattr(settings, "KAFKA_TASK_EVENTS_ENABLED", False):
        return None
    if settings.APP_ENVIRONMENT != "shadow":
        raise ValueError("Kafka task events require shadow environment")
    run_id = str(settings.LOAD_TEST_RUN_ID or "").strip()
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("Kafka task events require a KSHADOW run id")
    return {"environment": "shadow", "run_id": run_id}


def _public_changed_fields(changed_fields: Iterable[str] | None) -> list[str]:
    if changed_fields is None:
        return []
    if isinstance(changed_fields, (str, bytes, bytearray, dict)):
        raise ValueError("changed fields must be a sequence")
    result: set[str] = set()
    for field in changed_fields:
        mapped = _FIELD_MAP.get(str(field))
        if mapped is not None:
            result.add(mapped)
    return sorted(result)


def build_task_event(
    settings: Settings,
    *,
    event_id: str,
    event_type: str,
    task_id: str,
    source_id: int,
    revision: int,
    operation_id: str,
    changed_fields: Iterable[str] | None,
    occurred_at: datetime | None = None,
    kafka_event_type: str | None = None,
) -> dict[str, Any]:
    config = bridge_config(settings)
    if config is None:
        raise ValueError("Kafka task event bridge is disabled")
    mapped_type = _EVENT_TYPE_MAP.get(event_type)
    if mapped_type is None:
        raise ValueError("unsupported task event type")
    if kafka_event_type is not None:
        if (event_type, kafka_event_type) != ("online.task.deleted", "task.archived"):
            raise ValueError("unsupported explicit task event mapping")
        mapped_type = kafka_event_type
    timestamp = occurred_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    if isinstance(source_id, bool) or not isinstance(source_id, int):
        raise ValueError("source_id must be an integer")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ValueError("revision must be an integer")
    event = {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": mapped_type,
        "task_id": task_id,
        "source_id": source_id,
        "revision": revision,
        "operation_id": operation_id,
        "changed_fields": _public_changed_fields(changed_fields),
        "timestamp": timestamp.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
        **config,
    }
    return validate_task_event(event)


async def enqueue_task_event(
    cur,
    *,
    settings: Settings,
    domain: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | int,
    aggregate_revision: int,
    audiences: Iterable[str],
    task_id: str,
    source_id: int,
    revision: int,
    operation_id: str,
    changed_fields: Iterable[str] | None = None,
    occurred_at: datetime | None = None,
    event_id: str | None = None,
    kafka_event_type: str | None = None,
) -> str:
    """Insert the domain outbox and, when enabled, its Kafka delivery intent."""
    config = bridge_config(settings)
    event_id = event_id or str(uuid.uuid4())
    kafka_event = (
        build_task_event(
            settings,
            event_id=event_id,
            event_type=event_type,
            task_id=task_id,
            source_id=source_id,
            revision=revision,
            operation_id=operation_id,
            changed_fields=changed_fields,
            occurred_at=occurred_at,
            kafka_event_type=kafka_event_type,
        )
        if config is not None
        else None
    )
    return await enqueue_event(
        cur,
        domain=domain,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_revision=aggregate_revision,
        audiences=audiences,
        task_id=task_id,
        source_id=source_id,
        operation_id=operation_id,
        changed_fields=changed_fields,
        event_id=event_id,
        occurred_at=occurred_at,
        kafka_event=kafka_event,
        kafka_run_id=config["run_id"] if config is not None else None,
    )
