"""Closed routing table for task and auxiliary metadata contracts.

Only validated metadata crosses this boundary. Topic names never come from
payload fields, queue names, caller input or external writeback destinations.
"""
from services.kafka_event_contract import (
    EVENT_TYPES, EventContractError, validate_task_event, serialize_task_event,
    partition_key_bytes,
)

AUXILIARY_TOPICS = {
    'photo.writeback.requested': 'binhu.photo.events',
    'venue.sync.requested': 'binhu.venue.events',
}


def delivery_topic(event_type: str, channel: str) -> str:
    if channel not in {'events', 'dlq'}:
        raise EventContractError('unsupported delivery channel')
    if event_type in EVENT_TYPES:
        base = 'binhu.task.events'
    elif event_type in AUXILIARY_TOPICS:
        base = AUXILIARY_TOPICS[event_type]
    else:
        raise EventContractError('unsupported event type')
    return base + ('.v1' if channel == 'events' else '.dlq.v1')


def validate_event(event: dict) -> dict:
    if not isinstance(event, dict):
        raise EventContractError('event must be an object')
    event_type = event.get('event_type')
    if not isinstance(event_type, str):
        raise EventContractError('unsupported event type')
    if event_type in EVENT_TYPES:
        return validate_task_event(event)
    if event_type in AUXILIARY_TOPICS:
        from services.kafka_aux_outbox_contract import validate_aux_event
        return validate_aux_event(event)
    raise EventContractError('unsupported event type')


def serialize_event(event: dict) -> bytes:
    normalized = validate_event(event)
    if normalized['event_type'] in EVENT_TYPES:
        return serialize_task_event(normalized)
    from services.kafka_aux_outbox_contract import serialize_aux_event
    return serialize_aux_event(normalized)


def event_partition_key(event: dict) -> bytes:
    normalized = validate_event(event)
    if normalized['event_type'] in EVENT_TYPES:
        return partition_key_bytes(normalized)
    from services.kafka_aux_outbox_contract import partition_key
    return partition_key(normalized)
