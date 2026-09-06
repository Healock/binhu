"""Synthetic ledger-to-Kafka component test (not Backend business acceptance)."""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from relay_runtime import configuration, connect_shadow
from services.kafka_delivery_store import MySQLDeliveryStore, enqueue_delivery
from services.kafka_relay import KafkaRelay, EVENT_TOPIC


async def main():
    config=configuration()
    pool=await connect_shadow(config)
    events=[dict(schema_version=1,event_id=str(uuid.uuid4()),event_type="task.saved",
                 task_id=f"t_fullchain:{910000+i}",source_id=910000+i,revision=1,
                 operation_id=str(uuid.uuid4()),changed_fields=["task_state"],
                 timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
                 environment="shadow",run_id=config["run_id"]) for i in range(12)]
    expected={e["event_id"]:e for e in events}
    producer=AIOKafkaProducer(bootstrap_servers=config["bootstrap"],enable_idempotence=True,acks="all")
    consumer=AIOKafkaConsumer(EVENT_TOPIC,bootstrap_servers=config["bootstrap"],
                             group_id="poc-roundtrip-"+str(uuid.uuid4()),auto_offset_reset="earliest",
                             enable_auto_commit=False)
    try:
        async with pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                for event in events: await enqueue_delivery(cur,event,run_id=config["run_id"])
                placeholders=','.join(['%s']*len(events))
                await cur.execute(f"SELECT status FROM _kafka_event_delivery WHERE event_id IN ({placeholders})",tuple(expected))
                rows=await cur.fetchall()
                assert len(rows)==12 and all(r[0]=="pending" for r in rows)
            await conn.commit()
        await asyncio.wait_for(producer.start(),30)
        await asyncio.wait_for(consumer.start(),30)
        relay=KafkaRelay(MySQLDeliveryStore(pool,run_id=config["run_id"]),producer)
        for _ in events: assert await relay.run_once()=="published"
        assert await relay.run_once()=="idle"
        found={}
        deadline=asyncio.get_running_loop().time()+40
        while set(found)!=set(expected) and asyncio.get_running_loop().time()<deadline:
            batches=await consumer.getmany(timeout_ms=1000,max_records=100)
            for records in batches.values():
                for record in records:
                    event=json.loads(record.value)
                    if event.get("event_id") in expected:
                        assert event==expected[event["event_id"]]
                        assert record.key==f"{event['task_id']}|{event['source_id']}".encode()
                        assert event["event_id"] not in found, "unexpected duplicate in no-fault roundtrip"
                        found[event["event_id"]]={"partition":record.partition,"offset":record.offset}
        assert set(found)==set(expected), "not all committed deliveries reached Kafka"
        print(json.dumps({"status":"passed","scope":"synthetic_ledger_to_kafka_component",
                          "run_id":config["run_id"],"sent":len(events),"consumed":len(found),
                          "pending_before_commit":True,"ledger_drained":True,"records":found}),flush=True)
    finally:
        await consumer.stop(); await producer.stop()
        pool.close(); await pool.wait_closed()


if __name__=="__main__": asyncio.run(main())
