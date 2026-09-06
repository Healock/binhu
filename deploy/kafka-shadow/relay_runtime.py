"""Entrypoint exclusively for the independent shadow delivery ledger."""
from __future__ import annotations
import asyncio
import json
import os
import re


def configuration(environ=None):
    env = os.environ if environ is None else environ
    run = env.get("LOAD_TEST_RUN_ID", "")
    database = env.get("MYSQL_DATABASE", "")
    if env.get("APP_ENVIRONMENT") != "shadow" or not re.fullmatch(r"KSHADOW-[A-Za-z0-9_-]{1,64}", run):
        raise ValueError("shadow relay identity required")
    if not re.fullmatch(r"KShadow_[A-Za-z0-9_]{1,50}", database):
        raise ValueError("isolated shadow database required")
    if env.get("MYSQL_HOST") != "derived-mysql" or env.get("MYSQL_USER") != "shadow_derived":
        raise ValueError("isolated relay database target required")
    if not env.get("MYSQL_PASSWORD") or len(env["MYSQL_PASSWORD"]) < 24:
        raise ValueError("independent relay database credential required")
    bootstrap = env.get("KAFKA_BOOTSTRAP_SERVERS", "")
    if bootstrap != "kafka-1:9092,kafka-2:9092,kafka-3:9092":
        raise ValueError("isolated Kafka target required")
    return {"run_id": run, "database": database, "password": env["MYSQL_PASSWORD"], "bootstrap": bootstrap}


async def connect_shadow(config):
    import aiomysql
    pool = await aiomysql.create_pool(
        host="derived-mysql", port=3306, user="shadow_derived",
        password=config["password"], db=config["database"],
        minsize=1, maxsize=2, connect_timeout=5, autocommit=True,
        charset="utf8mb4", init_command="SET time_zone='+00:00'",
    )
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT DATABASE()")
                if await cur.fetchone() != (config["database"],):
                    raise ValueError("database identity mismatch")
                await cur.execute("SELECT environment,run_id,database_name FROM _shadow_identity")
                rows = await cur.fetchall()
                if list(rows) != [("shadow", config["run_id"], config["database"])]:
                    raise ValueError("shadow marker mismatch")
        return pool
    except BaseException:
        pool.close()
        await pool.wait_closed()
        raise


async def main():
    from aiokafka import AIOKafkaProducer
    from services.kafka_delivery_store import MySQLDeliveryStore
    from services.kafka_relay import KafkaRelay
    config = configuration()
    pool = await connect_shadow(config)
    producer = AIOKafkaProducer(
        bootstrap_servers=config["bootstrap"], enable_idempotence=True,
        acks="all", request_timeout_ms=15000, retry_backoff_ms=500,
        client_id="relay-" + config["run_id"],
    )
    try:
        await asyncio.wait_for(producer.start(), timeout=30)
        relay = KafkaRelay(MySQLDeliveryStore(pool, run_id=config["run_id"]), producer)
        failures = 0
        while True:
            try:
                state = await relay.run_once()
                failures = 0
                if state != "idle":
                    print(json.dumps({"run_id": config["run_id"], "state": state}), flush=True)
                await asyncio.sleep(0.5 if state == "idle" else 0.01)
            except Exception as exc:
                failures += 1
                print(json.dumps({"state": "store_or_runtime_error", "error_type": type(exc).__name__, "consecutive": failures}), flush=True)
                if failures >= 3:
                    raise RuntimeError("relay paused after three runtime failures") from None
                await asyncio.sleep(2)
    finally:
        try:
            await asyncio.wait_for(producer.stop(), timeout=30)
        finally:
            pool.close()
            await pool.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
