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
        await ensure_database_identity(pool, config)
        return pool
    except BaseException:
        pool.close()
        await pool.wait_closed()
        raise


async def ensure_database_identity(pool, config):
    """Verify and rebind the retained Dev database to the current run ID."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT environment,run_id,database_name FROM _pipeline_identity WHERE id=1")
            row = await cur.fetchone()
            if row is None or row[0] != "development" or row[2] != config["MYSQL_DATABASE"]:
                raise ValueError("database identity mismatch")
            if row[1] != config["DEV_RUN_ID"]:
                await cur.execute(
                    "UPDATE _pipeline_identity SET run_id=%s WHERE id=1 AND environment=%s AND database_name=%s",
                    (config["DEV_RUN_ID"], "development", config["MYSQL_DATABASE"]),
                )


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


async def python_metadata_worker(config):
    from .services.python_metadata_worker import run
    await run(config)


async def dual_track_monitor(config):
    from .dual_track_monitor import run
    evidence_dir = __import__("pathlib").Path(os.environ.get(
        "DUAL_TRACK_EVIDENCE_DIR", "/var/lib/binhu-dev-event-pipeline/evidence")) / config["DEV_RUN_ID"]
    evidence_id = os.environ.get("DUAL_TRACK_EVIDENCE_ID", "dual-track-" + config["DEV_RUN_ID"][4:])
    await run(config, evidence_dir, evidence_id)


def backend_relay_configuration(environ=None):
    """Validate the Dev-only Backend outbox relay targets.

    This relay deliberately uses the Dev Backend database and Redis stream;
    it never shares the pipeline database credentials or Production relay.
    """
    env = os.environ if environ is None else environ
    if env.get("APP_ENVIRONMENT") != "development":
        raise ValueError("development identity required")
    run_id = env.get("DEV_RUN_ID", "")
    if not re.fullmatch(r"dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("Dev run ID required")
    host = env.get("BACKEND_MYSQL_HOST", "")
    database = env.get("BACKEND_MYSQL_DATABASE", "")
    user = env.get("BACKEND_MYSQL_USER", "")
    password = env.get("BACKEND_MYSQL_PASSWORD", "")
    redis_url = env.get("BACKEND_REDIS_URL", "")
    if host != "environment-mysql" or not re.fullmatch(r"Dev_[A-Za-z0-9_]+", database):
        raise ValueError("isolated Dev Backend database required")
    if user != "environment_app" or not password or len(password) < 32:
        raise ValueError("independent Backend credential required")
    if not redis_url or any(value in redis_url.lower() for value in ("production", "staging", "shadow")):
        raise ValueError("isolated Dev Backend Redis required")
    return {
        "APP_ENVIRONMENT": "development", "DEV_RUN_ID": run_id,
        "BACKEND_MYSQL_HOST": host, "BACKEND_MYSQL_DATABASE": database,
        "BACKEND_MYSQL_USER": user, "BACKEND_MYSQL_PASSWORD": password,
        "BACKEND_REDIS_URL": redis_url,
        "BACKEND_REDIS_STREAM_KEY": env.get("BACKEND_REDIS_STREAM_KEY", "binhu:events"),
    }


async def backend_outbox_relay(config):
    from .services.backend_outbox_relay import BackendOutboxRelay
    import aiomysql
    from redis.asyncio import Redis

    pool = await aiomysql.create_pool(
        host=config["BACKEND_MYSQL_HOST"], port=3306,
        user=config["BACKEND_MYSQL_USER"], password=config["BACKEND_MYSQL_PASSWORD"],
        db=config["BACKEND_MYSQL_DATABASE"], minsize=1, maxsize=2,
        connect_timeout=5, autocommit=True, charset="utf8mb4",
        init_command="SET time_zone='+00:00'",
    )
    client = Redis.from_url(config["BACKEND_REDIS_URL"], decode_responses=True,
                            socket_timeout=10, socket_connect_timeout=5)
    try:
        await client.ping()
        worker = BackendOutboxRelay(pool, client, config)
        while True:
            state = await worker.run_once()
            if state != "idle":
                print(json.dumps({"component": "dev-backend-outbox-relay", "state": state}), flush=True)
            await asyncio.sleep(.5 if state == "idle" else .01)
    finally:
        await client.aclose()
        pool.close()
        await pool.wait_closed()


async def main(mode):
    if mode == "backend-outbox-relay":
        await backend_outbox_relay(backend_relay_configuration())
        return
    config = configuration()
    from .schema_registry import verify
    await asyncio.to_thread(verify)
    if mode == "python-metadata-worker":
        await python_metadata_worker(config)
        return
    if mode == "dual-track-monitor":
        await dual_track_monitor(config)
        return
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
    parser.add_argument("mode", choices=("relay", "bridge", "business-bridge", "backend-outbox-relay", "python-metadata-worker", "dual-track-monitor"))
    args = parser.parse_args()
    try:
        asyncio.run(main(args.mode))
    except Exception:
        # Connector exception text can contain credentials and SQL values.
        raise SystemExit("Dev pipeline stopped; identity or runtime check failed") from None
