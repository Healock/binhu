"""Closed Dev-only task metadata routing; external writeback is unsupported."""
from .kafka_event_contract import (
    EVENT_TYPES, EventContractError, validate_task_event,
    serialize_task_event, partition_key_bytes,
)

EVENT_TOPIC = "dev.task.events.v1"
DLQ_TOPIC = "dev.task.events.dlq.v1"


def delivery_topic(event_type: str, channel: str) -> str:
    if event_type not in EVENT_TYPES or channel not in {"events", "dlq"}:
        raise EventContractError("unsupported Dev delivery route")
    return EVENT_TOPIC if channel == "events" else DLQ_TOPIC


validate_event = validate_task_event
serialize_event = serialize_task_event
event_partition_key = partition_key_bytes
