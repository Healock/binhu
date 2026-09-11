"""Dev metadata relay and derived cache bridge. No production imports or URLs."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from .services.kafka_delivery_store import MySQLDeliveryStore
from .services.kafka_relay import KafkaRelay
from .services.derived_revision_cache import RevisionCache


def configuration(environ=None):
    env = os.environ if environ is None else environ
    run_id = env.get("DEV_RUN_ID", "")
    if env.get("APP_ENVIRONMENT") != "development" or not re.fullmatch(r"dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("Dev identity required")
    targets = {
        "MYSQL_HOST": "dev-derived-mysql", "MYSQL_DATABASE": "Dev_EventPipeline",
        "MYSQL_USER": "dev_pipeline", "REDIS_HOST": "dev-derived-redis",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9092,kafka-2:9092,kafka-3:9092",
    }
    if any(env.get(k) != v for k, v in targets.items()):
        raise ValueError("isolated Dev targets required")
    for key in ("MYSQL_PASSWORD", "REDIS_PASSWORD"):
        if not re.fullmatch(r"[0-9a-f]{48}", env.get(key, "")):
            raise ValueError("independent runtime credential required")
    backend_url = env.get("BACKEND_REDIS_URL", "")
    if "production" in backend_url.lower() or "staging" in backend_url.lower():
        raise ValueError("external environment Redis is forbidden")
    return {**targets, "DEV_RUN_ID": run_id,
            "MYSQL_PASSWORD": env["MYSQL_PASSWORD"], "REDIS_PASSWORD": env["REDIS_PASSWORD"],
            "BACKEND_REDIS_URL": backend_url,
            "BACKEND_REDIS_STREAM_KEY": env.get("BACKEND_REDIS_STREAM_KEY", "binhu:events"),
            "BACKEND_REDIS_START_ID": env.get("BACKEND_REDIS_START_ID", "$")}


async def connect(config):
    import aiomysql
    pool = await aiomysql.create_pool(
        host=config["MYSQL_HOST"], port=3306, user=config["MYSQL_USER"],
        password=config["MYSQL_PASSWORD"], db=config["MYSQL_DATABASE"],
        minsize=1, maxsize=2, connect_timeout=5, autocommit=True,
        charset="utf8mb4", init_command="SET time_zone='+00:00'",
    )
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT DATABASE()")
                if await cur.fetchone() != (config["MYSQL_DATABASE"],):
                    raise ValueError("database target mismatch")
                await cur.execute("SELECT environment,run_id,database_name FROM _pipeline_identity")
                if list(await cur.fetchall()) != [("development", config["DEV_RUN_ID"], config["MYSQL_DATABASE"])]:
                    raise ValueError("database identity mismatch")
        return pool
    except BaseException:
        pool.close()
        await pool.wait_closed()
        raise


async def relay(config, pool):
    from aiokafka import AIOKafkaProducer
    producer = AIOKafkaProducer(
        bootstrap_servers=config["KAFKA_BOOTSTRAP_SERVERS"], enable_idempotence=True,
        acks="all", request_timeout_ms=15000, client_id=config["DEV_RUN_ID"] + "-relay",
    )
    try:
        await asyncio.wait_for(producer.start(), 30)
        worker = KafkaRelay(MySQLDeliveryStore(pool, run_id=config["DEV_RUN_ID"]), producer)
        while True:
            state = await worker.run_once()
            if state != "idle":
                print(json.dumps({"component": "dev-relay", "state": state}), flush=True)
            await asyncio.sleep(.5 if state == "idle" else .01)
    finally:
        await asyncio.wait_for(producer.stop(), 30)


def cache_result(task_id, source_id, revision):
    """Derived metadata only; this is not a business address matching result."""
    content = f"{task_id}|{source_id}|{revision}".encode()
    return {"task_id": task_id, "source_id": source_id, "revision": revision,
            "source_revision": revision, "source": "flink-dev",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "content_hash": hashlib.sha256(content).hexdigest(),
            "fields": {"task_state": "metadata_processed"}}


async def bridge(config, pool):
    from redis.asyncio import Redis
    client = Redis(host=config["REDIS_HOST"], password=config["REDIS_PASSWORD"],
                   socket_timeout=5, socket_connect_timeout=5, decode_responses=True)
    cache = RevisionCache(client, config["DEV_RUN_ID"])
    try:
        while True:
            # Keyset pagination bounds memory and never logs task metadata.
            after_task, after_source = "", 0
            while True:
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT task_id,source_id,revision FROM dev_task_revisions "
                            "WHERE run_id=%s AND (task_id>%s OR (task_id=%s AND source_id>%s)) "
                            "ORDER BY task_id,source_id LIMIT 200",
                            (config["DEV_RUN_ID"], after_task, after_task, after_source),
                        )
                        rows = await cur.fetchall()
                if not rows:
                    break
                for task, source, revision in rows:
                    state = await cache.put(cache_result(task, source, revision))
                    if state == "conflict":
                        raise ValueError("derived revision conflict")
                after_task, after_source = rows[-1][:2]
            await asyncio.sleep(1)
    finally:
        await client.aclose()


async def business_bridge(config, pool):
    from .business_bridge import run
    await run(config, pool)


async def main(mode):
    config = configuration()
    from .schema_registry import verify
    await asyncio.to_thread(verify)
    pool = await connect(config)
    try:
        if mode == "relay":
            await relay(config, pool)
        elif mode == "bridge":
            await bridge(config, pool)
        else:
            await business_bridge(config, pool)
    finally:
        pool.close()
        await pool.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("relay", "bridge", "business-bridge"))
    args = parser.parse_args()
    try:
        asyncio.run(main(args.mode))
    except Exception:
        # Connector exception text can contain credentials and SQL values.
        raise SystemExit("Dev pipeline stopped; identity or runtime check failed") from None
