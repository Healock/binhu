"""Real SIGKILL and ACK/commit-window test against isolated MySQL and Kafka."""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from relay_runtime import configuration, connect_shadow
from services.kafka_delivery_store import MySQLDeliveryStore, enqueue_delivery
from services.kafka_relay import KafkaRelay, EVENT_TOPIC, LEASE_SECONDS


async def child():
    config = configuration()
    pool = await connect_shadow(config)
    store = MySQLDeliveryStore(pool, run_id=config["run_id"])
    producer = AIOKafkaProducer(bootstrap_servers=config["bootstrap"],
                               enable_idempotence=True, acks="all")
    await asyncio.wait_for(producer.start(), 30)
    delivery = await store.claim()
    assert delivery is not None and delivery.event_id == os.environ["CRASH_EVENT_ID"]
    await asyncio.wait_for(producer.send_and_wait(EVENT_TOPIC, value=delivery.payload,
                                                 key=delivery.key), 20)
    # Deliberately hold the ACK -> ledger commit window until parent SIGKILL.
    print(json.dumps({"phase":"acked_before_commit", "event_id":delivery.event_id}), flush=True)
    await asyncio.sleep(180)
    raise AssertionError("parent did not kill child")


async def main():
    config = configuration()
    pool = await connect_shadow(config)
    store = MySQLDeliveryStore(pool, run_id=config["run_id"])
    producer = AIOKafkaProducer(bootstrap_servers=config["bootstrap"],
                               enable_idempotence=True, acks="all")
    consumer = AIOKafkaConsumer(EVENT_TOPIC, bootstrap_servers=config["bootstrap"],
        group_id="crash-proof-"+str(uuid.uuid4()), enable_auto_commit=False,
        auto_offset_reset="earliest")
    events = [dict(schema_version=1,event_id=str(uuid.uuid4()),event_type="task.saved",
                   task_id=f"t_fullchain:{930000+i}",source_id=930000+i,revision=1,
                   operation_id=str(uuid.uuid4()),changed_fields=["task_state"],
                   timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
                   environment="shadow",run_id=config["run_id"]) for i in range(2)]
    process = None
    try:
        # Tests run exclusively; never claim another run's pending work.
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM _kafka_event_delivery WHERE run_id=%s AND status NOT IN ('published','dead_letter','blocked','quarantined')", (config["run_id"],))
                assert (await cur.fetchone())[0] == 0, "pending work prevents exclusive crash fixture"
            await conn.begin()
            async with conn.cursor() as cur:
                for event in events: await enqueue_delivery(cur,event,run_id=config["run_id"])
            await conn.commit()
        await asyncio.wait_for(producer.start(),30)
        await asyncio.wait_for(consumer.start(),30)
        relay = KafkaRelay(store,producer)
        assert await relay.run_once() == "published"
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT event_id,status FROM _kafka_event_delivery WHERE event_id IN (%s,%s)", tuple(e["event_id"] for e in events))
                states=dict(await cur.fetchall())
        confirmed = next(k for k,v in states.items() if v=="published")
        uncertain = next(k for k,v in states.items() if v=="pending")
        env=dict(os.environ,CRASH_EVENT_ID=uncertain)
        process=await asyncio.create_subprocess_exec(sys.executable,__file__,"child",
            env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        line=await asyncio.wait_for(process.stdout.readline(),45)
        assert json.loads(line)=={"phase":"acked_before_commit","event_id":uncertain}
        process.kill()
        assert await asyncio.wait_for(process.wait(),10)==-9
        assert await store.claim() is None, "unexpired lease must remain exclusive"
        print(json.dumps({"phase":"real_lease_expiry_wait","seconds":LEASE_SECONDS+2}),flush=True)
        await asyncio.sleep(LEASE_SECONDS+2)
        assert await relay.run_once()=="published"
        assert await relay.run_once()=="idle"
        counts={confirmed:0,uncertain:0}
        deadline=asyncio.get_running_loop().time()+30
        while asyncio.get_running_loop().time()<deadline:
            batches=await consumer.getmany(timeout_ms=1000,max_records=100)
            for records in batches.values():
                for record in records:
                    event=json.loads(record.value)
                    if event.get("event_id") in counts: counts[event["event_id"]]+=1
            if counts[confirmed]>=1 and counts[uncertain]>=2: break
        assert counts=={confirmed:1,uncertain:2}
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT event_id,status,event_attempts FROM _kafka_event_delivery WHERE event_id IN (%s,%s)",(confirmed,uncertain))
                final={eid:{"status":state,"attempts":attempts} for eid,state,attempts in await cur.fetchall()}
        assert final[confirmed]==dict(status="published",attempts=1)
        assert final[uncertain]==dict(status="published",attempts=2)
        print(json.dumps({"status":"passed","scope":"synthetic_ledger_real_sigkill_ack_window",
                          "run_id":config["run_id"],"confirmed_event_sent_once":True,
                          "uncertain_ack_replayed":True,"lease_seconds":LEASE_SECONDS,
                          "consumer_counts":counts,"ledger":final}),flush=True)
    finally:
        if process and process.returncode is None:
            process.kill();await process.wait()
        await consumer.stop()
        await producer.stop()
        pool.close();await pool.wait_closed()


if __name__=="__main__":
    asyncio.run(child() if len(sys.argv)>1 else main())
