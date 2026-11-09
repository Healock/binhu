"""Closed Dev-only task metadata routing; external writeback is unsupported."""
from .kafka_event_contract import (
    EVENT_TYPES, EventContractError, validate_task_event,
    serialize_task_event, partition_key_bytes,
)
from .online_summary_contract import (
    SUMMARY_EVENT_TYPES, validate_summary_fact, OnlineSummaryContractError,
)
from ..identity import summary_topic_for as registered_summary_topic_for
from ..domain_pipeline import domain_for_event

EVENT_TOPIC = "dev.task.read-model.v4"
DLQ_TOPIC = "dev.task.read-model.dlq.v4"
V3_EVENT_TOPIC = "dev.task.read-model.v3"
V3_DLQ_TOPIC = "dev.task.read-model.dlq.v3"
LEGACY_EVENT_TOPIC = "dev.task.events.v1"
LEGACY_DLQ_TOPIC = "dev.task.events.dlq.v1"


def summary_topic_for(environment: str, *, dead_letter: bool = False) -> str:
    return registered_summary_topic_for(environment, dead_letter=dead_letter)


def delivery_topic(
    event_type: str,
    channel: str,
    schema_version: int = 4,
    environment: str = "development",
) -> str:
    if channel not in {"events", "dlq"}:
        raise EventContractError("unsupported Dev delivery route")
    if environment not in {"development", "staging"}:
        raise EventContractError("non-Production pipeline topic required")
    if event_type in SUMMARY_EVENT_TYPES:
        if schema_version != 1:
            raise OnlineSummaryContractError("unsupported online summary schema")
        domain = domain_for_event(event_type)
        return domain.topic(environment, "dlq" if channel == "dlq" else "events")
    if event_type not in EVENT_TYPES:
        raise EventContractError("unsupported Dev delivery route")
    domain = domain_for_event(event_type)
    if schema_version == 1:
        if environment != "development":
            raise EventContractError("legacy v1 topic is Development-only")
        return LEGACY_EVENT_TOPIC if channel == "events" else LEGACY_DLQ_TOPIC
    if schema_version == 3:
        if environment != "development":
            raise EventContractError("legacy v3 topic is Development-only")
        return V3_EVENT_TOPIC if channel == "events" else V3_DLQ_TOPIC
    if schema_version not in {4, 5, 6}:
        raise EventContractError("unsupported Dev delivery schema")
    return domain.topic(environment, "events" if channel == "events" else "dlq")


def validate_event(event):
    if isinstance(event, dict) and event.get("event_type") in SUMMARY_EVENT_TYPES:
        return validate_summary_fact(event)
    return validate_task_event(event)


def serialize_event(event):
    normalized = validate_event(event)
    if normalized.get("event_type") in SUMMARY_EVENT_TYPES:
        import json
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(payload) > 64 * 1024:
            raise OnlineSummaryContractError("online summary event size exceeded")
        return payload
    return serialize_task_event(normalized)


def event_partition_key(event):
    if isinstance(event, dict) and event.get("event_type") in SUMMARY_EVENT_TYPES:
        return validate_summary_fact(event)["row_key_hmac"].encode("ascii")
    return partition_key_bytes(event)
