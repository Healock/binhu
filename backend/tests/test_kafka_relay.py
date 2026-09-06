"""Crash and retry semantics; MySQL integration is a separate shadow check."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.kafka_relay import Delivery, KafkaRelay, retry_delay


class Store:
    def __init__(self, attempt=1, channel="events"):
        self.delivery = Delivery("event", b"{}", b"task", "lease", attempt, channel)
        self.completed = []

    async def claim(self):
        return self.delivery

    async def finish(self, delivery, **result):
        self.completed.append(result)
        return True


class Producer:
    def __init__(self, failure=None):
        self.failure = failure
        self.sent = []

    async def send_and_wait(self, topic, *, value, key):
        self.sent.append((topic, value, key))
        if self.failure:
            raise self.failure


def test_ack_is_required_before_published():
    store = Store()
    producer = Producer(TimeoutError("must not log credential-shaped raw error"))
    asyncio.run(KafkaRelay(store, producer).run_once())
    assert store.completed[0]["status"] == "retry"
    assert store.completed[0]["error_code"] == "transport_timeout"
    assert "credential" not in str(store.completed)


def test_success_publishes_exact_metadata_with_task_key():
    store, producer = Store(), Producer()
    asyncio.run(KafkaRelay(store, producer).run_once())
    assert producer.sent == [("binhu.task.events.v1", b"{}", b"task")]
    assert store.completed == [{"status": "published", "error_code": "", "delay_seconds": 0}]


def test_retry_budget_moves_to_durable_dlq_pending():
    store, producer = Store(attempt=5), Producer(ConnectionError("offline"))
    asyncio.run(KafkaRelay(store, producer).run_once())
    assert store.completed[0]["status"] == "dlq_pending"
    assert len(producer.sent) == 1  # DLQ gets its own leased attempt.


def test_dlq_failure_is_bounded_and_never_falsely_acknowledged():
    store, producer = Store(attempt=5, channel="dlq"), Producer(ConnectionError("offline"))
    asyncio.run(KafkaRelay(store, producer).run_once())
    assert producer.sent[0][0] == "binhu.task.events.dlq.v1"
    assert store.completed[0]["status"] == "blocked"


def test_dlq_success_records_dead_letter_only_after_ack():
    store, producer = Store(channel="dlq"), Producer()
    asyncio.run(KafkaRelay(store, producer).run_once())
    assert store.completed[0]["status"] == "dead_letter"


def test_cancellation_preserves_lease_for_crash_recovery():
    store, producer = Store(), Producer(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(KafkaRelay(store, producer).run_once())
    assert store.completed == []


def test_database_ack_failure_does_not_claim_transport_failure():
    class FailingStore(Store):
        async def finish(self, delivery, **result):
            raise RuntimeError("database commit unavailable")
    store, producer = FailingStore(), Producer()
    with pytest.raises(RuntimeError, match="database commit"):
        asyncio.run(KafkaRelay(store, producer).run_once())
    assert len(producer.sent) == 1


def test_stale_lease_cannot_report_success():
    class LostLeaseStore(Store):
        async def finish(self, delivery, **result):
            return False
    assert asyncio.run(KafkaRelay(LostLeaseStore(), Producer()).run_once()) == "lease_lost"


def test_retry_delays_are_finite():
    assert retry_delay(1, jitter=False) == 1
    assert retry_delay(999, jitter=False) == 60
    assert 48 <= retry_delay(999) <= 72
