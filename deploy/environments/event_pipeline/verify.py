"""Synthetic Dev delivery acceptance; requires the prepared database marker."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

from .runtime import configuration, connect
from .services.kafka_delivery_store import enqueue_delivery
from .services.derived_revision_cache import RevisionCache


def fixture(run_id, revision):
    # Reserved synthetic reference; no production task or person is inspected.
    task_id = "t_fullchain:900000000000000001"
    return {"schema_version": 1, "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}/{revision}")),
            "event_type": "task.saved", "task_id": task_id, "source_id": 900000000000000001,
            "revision": revision, "operation_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, run_id)),
            "changed_fields": ["task_state"], "timestamp": "2026-09-10T00:00:00Z",
            "environment": "development", "run_id": run_id}


async def run(mode):
    config = configuration()
    pool = await connect(config)
    try:
        if mode == "seed":
            async with pool.acquire() as conn:
                try:
                    await conn.begin()
                    async with conn.cursor() as cur:
                        # Duplicate enqueue plus out-of-order revisions.
                        for revision in (1, 3, 2, 3):
                            await enqueue_delivery(cur, fixture(config["DEV_RUN_ID"], revision), run_id=config["DEV_RUN_ID"])
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise
            return {"environment": "development", "run_id": config["DEV_RUN_ID"], "unique_events": 3, "enqueued": True}
        from redis.asyncio import Redis
        client = Redis(host=config["REDIS_HOST"], password=config["REDIS_PASSWORD"],
                       decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        try:
            if mode == "business":
                event_id = os.environ.get("DEV_ACCEPT_EVENT_ID", "")
                task_id = os.environ.get("DEV_ACCEPT_TASK_ID", "")
                source_id = int(os.environ.get("DEV_ACCEPT_SOURCE_ID", "0"))
                revision = int(os.environ.get("DEV_ACCEPT_REVISION", "0"))
                expected = {"event_id": event_id, "task_id": task_id,
                            "source_id": source_id, "revision": revision}
                if not event_id or not task_id or source_id <= 0 or revision <= 0:
                    raise ValueError("business acceptance inputs required")
            else:
                expected = fixture(config["DEV_RUN_ID"], 3)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT revision FROM dev_task_revisions WHERE run_id=%s AND task_id=%s AND source_id=%s",
                                      (config["DEV_RUN_ID"], expected["task_id"], expected["source_id"]))
                    row = await cur.fetchone()
                    await cur.execute("SELECT status FROM _kafka_event_delivery WHERE run_id=%s AND event_id=%s",
                                      (config["DEV_RUN_ID"], expected["event_id"]))
                    delivery = await cur.fetchone()
                    if mode != "business":
                        await cur.execute("SELECT status,COUNT(*) FROM _kafka_event_delivery WHERE run_id=%s GROUP BY status", (config["DEV_RUN_ID"],))
                        states = dict(await cur.fetchall())
                    else:
                        states = {delivery[0]: 1} if delivery else {}
            cache = RevisionCache(client, config["DEV_RUN_ID"])
            if mode == "business":
                value = await cache.get(expected["task_id"], expected["source_id"], "flink-dev", expected["revision"])
                passed = row == (expected["revision"],) and delivery == ("published",) and value is not None
                report = {"environment": "development", "run_id": config["DEV_RUN_ID"],
                          "business_event_id": expected["event_id"], "delivery_published": delivery == ("published",),
                          "mysql_revision_correct": row == (expected["revision"],),
                          "redis_revision_correct": value is not None,
                          "business_integration_verified": passed}
            else:
                value = await cache.get(expected["task_id"], expected["source_id"], "flink-dev", 3)
                passed = row == (3,) and value is not None and states == {"published": 3}
                report = {"environment": "development", "run_id": config["DEV_RUN_ID"],
                          "mysql_revision_correct": row == (3,), "redis_revision_correct": value is not None,
                          "delivery_counts": states, "minimal_event_flow_passed": passed,
                          "checkpoint_recovery_verified": False, "business_integration_verified": False}
            if not passed:
                raise ValueError("Dev event flow not converged")
            return report
        finally:
            await client.aclose()
    finally:
        pool.close()
        await pool.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "verify", "business"))
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args.mode))))
    except Exception:
        raise SystemExit("Dev event acceptance failed; preserve private diagnostics") from None
