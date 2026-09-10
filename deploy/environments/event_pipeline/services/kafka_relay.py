"""Independent at-least-once Kafka delivery state machine.

The store must durably lease a delivery and fence completion by its lease token.
Business callers insert metadata and delivery intent in their own transaction.
An acknowledged message may be replayed after a crash before database commit;
consumers must deduplicate event_id and fence revision. This module neither
starts the existing Redis relay nor changes its Outbox status.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any
from .kafka_envelope import delivery_topic, EVENT_TOPIC, DLQ_TOPIC
MAX_ATTEMPTS = 5
SEND_TIMEOUT_SECONDS = 20
LEASE_SECONDS = 90


@dataclass(frozen=True)
class Delivery:
    event_id: str
    payload: bytes
    key: bytes
    lease_token: str
    attempt: int
    channel: str
    event_type: str = "task.saved"


def retry_delay(attempt: int, *, jitter: bool = True) -> float:
    base = (1, 2, 5, 15, 60)[min(max(attempt - 1, 0), 4)]
    return base * random.uniform(0.8, 1.2) if jitter else float(base)


def classify_error(exc: Exception) -> str:
    # Persist only fixed codes, never exception text which may contain secrets.
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or name in {"KafkaTimeoutError", "RequestTimedOutError"}:
        return "transport_timeout"
    if name in {"AuthenticationFailedError", "SaslAuthenticationFailedError", "TopicAuthorizationFailedError", "GroupAuthorizationFailedError"}:
        return "authentication_or_authorization"
    if isinstance(exc, ConnectionError) or name in {"KafkaConnectionError", "NoBrokersAvailable", "NotEnoughReplicasError", "NotEnoughReplicasAfterAppendError"}:
        return "broker_unavailable"
    return "producer_failure"


class KafkaRelay:
    def __init__(self, store: Any, producer: Any) -> None:
        self.store = store
        self.producer = producer

    async def run_once(self) -> str:
        delivery = await self.store.claim()
        if delivery is None:
            return "idle"
        if delivery.channel not in {"events", "dlq"}:
            raise ValueError("unsupported delivery channel")
        topic = delivery_topic(delivery.event_type, delivery.channel)
        try:
            await asyncio.wait_for(
                self.producer.send_and_wait(topic, value=delivery.payload, key=delivery.key),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            error_code = classify_error(exc)
            if delivery.channel == "events":
                status = "dlq_pending" if delivery.attempt >= MAX_ATTEMPTS else "retry"
            else:
                status = "blocked" if delivery.attempt >= MAX_ATTEMPTS else "dlq_pending"
            delay = retry_delay(delivery.attempt)
        else:
            status = "published" if delivery.channel == "events" else "dead_letter"
            error_code, delay = "", 0
        # Keep DB completion outside the producer exception handler. A failed
        # commit is an uncertain ACK window, not a Kafka transport failure.
        owned = await self.store.finish(
            delivery, status=status, error_code=error_code, delay_seconds=delay,
        )
        return status if owned else "lease_lost"
