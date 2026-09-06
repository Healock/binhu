import asyncio

import pytest

from services.kafka_relay import Delivery, KafkaRelay


class Store:
    def __init__(self, event_type, channel):
        self.delivery = Delivery('event', b'{}', b'venue:8', 'lease', 1, channel,
                                 event_type=event_type)
        self.finished = []

    async def claim(self):
        return self.delivery

    async def finish(self, delivery, **state):
        self.finished.append(state)
        return True


class Producer:
    def __init__(self):
        self.sent = []

    async def send_and_wait(self, topic, **kwargs):
        self.sent.append((topic, kwargs))


@pytest.mark.parametrize('event_type,topic', [
    ('photo.writeback.requested', 'binhu.photo.events'),
    ('venue.sync.requested', 'binhu.venue.events'),
    ('task.archived', 'binhu.task.events'),
])
@pytest.mark.parametrize('channel,suffix,status', [
    ('events', '.v1', 'published'), ('dlq', '.dlq.v1', 'dead_letter'),
])
def test_auxiliary_delivery_cannot_leak_into_task_topic(event_type, topic, channel, suffix, status):
    store, producer = Store(event_type, channel), Producer()
    assert asyncio.run(KafkaRelay(store, producer).run_once()) == status
    assert producer.sent == [(topic + suffix, {'value': b'{}', 'key': b'venue:8'})]


def test_unknown_type_is_rejected_without_send_or_ack():
    store, producer = Store('secret.arbitrary', 'events'), Producer()
    with pytest.raises(ValueError):
        asyncio.run(KafkaRelay(store, producer).run_once())
    assert producer.sent == [] and store.finished == []
