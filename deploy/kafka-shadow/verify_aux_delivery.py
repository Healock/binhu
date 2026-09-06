"""Real shadow source-Outbox/ledger atomicity and auxiliary Kafka routing.

This creates fictional rows in the isolated derived database only. It does
not invoke the Backend, Tencent or venue cloud workers and never updates a
source Outbox status to acknowledge Kafka. The operator must stop the other
relay for this run so it cannot compete with the verifier's leased deliveries.
"""
import asyncio
import json
import uuid

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from relay_runtime import configuration, connect_shadow
from services.kafka_aux_outbox_contract import photo_outbox_to_event, venue_outbox_to_event
from services.kafka_delivery_store import enqueue_delivery, MySQLDeliveryStore
from services.kafka_envelope import delivery_topic, event_partition_key
from services.kafka_relay import KafkaRelay


# Same source schemas as domain_schema.py; there are deliberately no business
# bodies, external credentials, or external destination configuration here.
PHOTO_SCHEMA = """CREATE TABLE IF NOT EXISTS photo_sheet_outbox (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, source_id BIGINT NOT NULL,
 work_order_id BIGINT NOT NULL, action VARCHAR(30) NOT NULL,
 status VARCHAR(30) NOT NULL DEFAULT 'pending',
 attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
 next_attempt_at DATETIME DEFAULT NULL, error_code VARCHAR(100) NOT NULL DEFAULT '',
 last_error VARCHAR(500) NOT NULL DEFAULT '', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uk_photo_sheet_outbox_action (work_order_id,action),
 INDEX idx_photo_sheet_outbox_due(status,next_attempt_at,updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
VENUE_SCHEMA = """CREATE TABLE IF NOT EXISTS _venue_cloud_outbox (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, venue_id BIGINT NOT NULL,
 config_revision BIGINT UNSIGNED NOT NULL, action VARCHAR(20) NOT NULL,
 request_id CHAR(36) NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'pending',
 attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
 next_attempt_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 last_error_code VARCHAR(100) DEFAULT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uk_venue_cloud_outbox_request(request_id),
 UNIQUE KEY uk_venue_cloud_outbox_revision(venue_id,config_revision),
 INDEX idx_venue_cloud_outbox_pending(status,next_attempt_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""


async def insert_sources(cur, run_id, fixture_id):
    await cur.execute(
        "INSERT INTO photo_sheet_outbox(source_id,work_order_id,action) VALUES (%s,%s,'append_request')",
        (fixture_id, fixture_id),
    )
    photo_id = cur.lastrowid
    await cur.execute('SELECT created_at FROM photo_sheet_outbox WHERE id=%s', (photo_id,))
    photo = photo_outbox_to_event(dict(outbox_id=photo_id, source_id=fixture_id,
        work_order_id=fixture_id, action='append_request', created_at=(await cur.fetchone())[0]), run_id=run_id)
    request_id = str(uuid.uuid4())
    await cur.execute(
        "INSERT INTO _venue_cloud_outbox(venue_id,config_revision,action,request_id) VALUES (%s,1,'update',%s)",
        (fixture_id, request_id),
    )
    venue_id = cur.lastrowid
    await cur.execute('SELECT created_at FROM _venue_cloud_outbox WHERE id=%s', (venue_id,))
    venue = venue_outbox_to_event(dict(request_id=request_id, venue_id=fixture_id,
        config_revision=1, action='update', created_at=(await cur.fetchone())[0]), run_id=run_id)
    for event in (photo, venue):
        await enqueue_delivery(cur, event, run_id=run_id)
    return (photo, venue), (photo_id, venue_id)


async def main():
    config = configuration()
    pool = await connect_shadow(config)
    producer = AIOKafkaProducer(bootstrap_servers=config['bootstrap'], enable_idempotence=True, acks='all')
    consumer = AIOKafkaConsumer('binhu.photo.events.v1', 'binhu.venue.events.v1',
        bootstrap_servers=config['bootstrap'], group_id='aux-verifier-'+str(uuid.uuid4()),
        auto_offset_reset='earliest', enable_auto_commit=False)
    fixture_id = 1_000_000_000 + uuid.uuid4().int % 1_000_000_000
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM _kafka_event_delivery WHERE run_id=%s AND status NOT IN ('published','dead_letter')", (config['run_id'],))
                assert (await cur.fetchone())[0] == 0, 'requires drained ledger'
                for sql in (PHOTO_SCHEMA, VENUE_SCHEMA):
                    await cur.execute(sql)
                await conn.begin()
                rolled_events, rolled_ids = await insert_sources(cur, config['run_id'], fixture_id)
                await conn.rollback()
                for table, row_id in zip(('photo_sheet_outbox','_venue_cloud_outbox'), rolled_ids):
                    await cur.execute('SELECT COUNT(*) FROM '+table+' WHERE id=%s', (row_id,))
                    assert (await cur.fetchone())[0] == 0
                for event in rolled_events:
                    await cur.execute('SELECT COUNT(*) FROM _kafka_event_delivery WHERE event_id=%s', (event['event_id'],))
                    assert (await cur.fetchone())[0] == 0
                await conn.begin()
                events, source_ids = await insert_sources(cur, config['run_id'], fixture_id)
                # Duplicate registration must preserve the same immutable intent.
                for event in events:
                    await enqueue_delivery(cur, event, run_id=config['run_id'])
                await conn.commit()
        await asyncio.wait_for(producer.start(), 30)
        await asyncio.wait_for(consumer.start(), 30)
        deadline = asyncio.get_running_loop().time()+40
        while len(consumer.assignment()) < 6 and asyncio.get_running_loop().time() < deadline:
            await consumer.getmany(timeout_ms=1000)
        assert len(consumer.assignment()) == 6, 'auxiliary consumer assignment incomplete'
        relay = KafkaRelay(MySQLDeliveryStore(pool, run_id=config['run_id']), producer)
        for _ in events:
            assert await relay.run_once() == 'published'
        assert await relay.run_once() == 'idle'
        expected = {event['event_id']: event for event in events}
        found = {}
        deadline = asyncio.get_running_loop().time()+40
        while len(found) < len(expected) and asyncio.get_running_loop().time() < deadline:
            batches = await consumer.getmany(timeout_ms=1000)
            for records in batches.values():
                for record in records:
                    event = json.loads(record.value)
                    if event.get('event_id') not in expected:
                        continue
                    assert event == expected[event['event_id']]
                    assert record.topic == delivery_topic(event['event_type'], 'events')
                    assert record.key == event_partition_key(event)
                    found[event['event_id']] = dict(topic=record.topic, partition=record.partition, offset=record.offset)
        assert set(found) == set(expected), 'committed source event missing in Kafka'
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for table, row_id in zip(('photo_sheet_outbox','_venue_cloud_outbox'), source_ids):
                    await cur.execute('SELECT status,attempt_count FROM '+table+' WHERE id=%s', (row_id,))
                    assert await cur.fetchone() == ('pending', 0), 'Kafka must not acknowledge external side effects'
                for event in events:
                    await cur.execute('SELECT status,event_attempts FROM _kafka_event_delivery WHERE event_id=%s', (event['event_id'],))
                    assert await cur.fetchone() == ('published', 1)
        print(json.dumps(dict(status='passed', scope='synthetic_aux_source_transaction_and_kafka',
            run_id=config['run_id'], rollback_source_and_ledger=True, duplicate_registration=True,
            external_outbox_status_unchanged=True, records=found)), flush=True)
    finally:
        await consumer.stop()
        await producer.stop()
        pool.close()
        await pool.wait_closed()


if __name__ == '__main__':
    asyncio.run(main())
