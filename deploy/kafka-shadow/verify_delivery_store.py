"""Real shadow MySQL transaction/lease checks, with synthetic metadata only."""
import asyncio
import json
import uuid
from datetime import datetime, timezone

from relay_runtime import configuration, connect_shadow
from services.kafka_delivery_store import SCHEMA_SQL, MySQLDeliveryStore, enqueue_delivery


async def main():
    config = configuration()
    pool = await connect_shadow(config)
    checks = {}
    event = dict(schema_version=1,event_id=str(uuid.uuid4()),event_type="task.saved",
                 task_id="t_fullchain:900001",source_id=900001,revision=5,
                 operation_id=str(uuid.uuid4()),changed_fields=["task_state"],
                 timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
                 environment="shadow",run_id=config["run_id"])
    store = MySQLDeliveryStore(pool, run_id=config["run_id"])
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(SCHEMA_SQL)
                await cur.execute("SELECT COUNT(*) FROM _kafka_event_delivery WHERE run_id=%s", (config["run_id"],))
                assert (await cur.fetchone())[0] == 0, "requires unused ledger"
                await conn.begin()
                await enqueue_delivery(cur,event,run_id=config["run_id"])
                await conn.rollback()
                await cur.execute("SELECT COUNT(*) FROM _kafka_event_delivery WHERE event_id=%s", (event["event_id"],))
                assert (await cur.fetchone())[0] == 0
                checks["rollback_leaves_no_delivery"] = True
                await conn.begin()
                await enqueue_delivery(cur,event,run_id=config["run_id"])
                await conn.commit()
                await cur.execute("SELECT status FROM _kafka_event_delivery WHERE event_id=%s", (event["event_id"],))
                assert (await cur.fetchone())[0] == "pending"
                checks["commit_is_pending"] = True
                await conn.begin()
                await enqueue_delivery(cur,event,run_id=config["run_id"])
                await conn.commit()
                changed=dict(event,revision=6)
                await conn.begin()
                try:
                    await enqueue_delivery(cur,changed,run_id=config["run_id"])
                except ValueError:
                    await conn.rollback()
                else:
                    raise AssertionError("conflicting duplicate was accepted")
                checks["duplicate_same_id_is_immutable"] = True
        first=await store.claim()
        assert first and first.attempt==1 and first.channel=="events"
        assert await store.claim() is None
        checks["active_lease_not_reclaimed"] = True
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE _kafka_event_delivery SET locked_until=DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 1 SECOND) WHERE event_id=%s AND run_id=%s",(event["event_id"],config["run_id"]))
        second=await store.claim()
        assert second and second.lease_token != first.lease_token and second.attempt==2
        assert not await store.finish(first,status="published",error_code="",delay_seconds=0)
        assert await store.finish(second,status="retry",error_code="synthetic_retry",delay_seconds=0)
        third=await store.claim()
        assert third and third.attempt==3
        assert await store.finish(third,status="published",error_code="",delay_seconds=0)
        assert await store.claim() is None
        checks["expired_lease_fenced_and_confirmed_not_reclaimed"] = True
        print(json.dumps({"run_id":config["run_id"],"status":"passed","checks":checks,
                          "event_id":event["event_id"],"scope":"ledger_only_no_kafka_ack"}),flush=True)
    finally:
        pool.close(); await pool.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
