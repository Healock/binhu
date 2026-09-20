"""Dev-only Python implementation of the task metadata projection."""
from __future__ import annotations

import json
import hashlib
from typing import Any

from .kafka_event_contract import validate_task_event
from .task_metadata_projection import IncrementalTaskMetadataProjector, flatten_projection, _canonical
from ..identity import topic_for

TABLE = "dev_task_metadata_python"


def event_ledger_sql(raw: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """Return the idempotent durable event ledger insert for one event."""
    event = validate_task_event(raw)
    digest = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
    return (
        "INSERT INTO dev_task_metadata_python_events "
        "(run_id,event_id,task_id,source_id,revision,canonical_sha256) "
        "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE event_id=event_id",
        (event["run_id"], event["event_id"], event["task_id"], event["source_id"], event["revision"], digest),
    )


def upsert_sql(row: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """Return the parameterized upsert for the isolated Python output table."""
    fields = flatten_projection(row)
    names = (
        "run_id", "task_id", "source_id", "revision", "event_count",
        "changed_field_count", "created_count", "saved_count", "claimed_count",
        "assigned_count", "reviewed_count", "archived_count", "deleted_count",
    )
    sql = (
        f"INSERT INTO {TABLE} (" + ",".join(names) + ") VALUES (" + ",".join(["%s"] * len(names)) + ") "
        "ON DUPLICATE KEY UPDATE revision=VALUES(revision),event_count=VALUES(event_count),"
        "changed_field_count=VALUES(changed_field_count),created_count=VALUES(created_count),"
        "saved_count=VALUES(saved_count),claimed_count=VALUES(claimed_count),"
        "assigned_count=VALUES(assigned_count),reviewed_count=VALUES(reviewed_count),"
        "archived_count=VALUES(archived_count),deleted_count=VALUES(deleted_count)"
    )
    return sql, tuple(fields[name] for name in names)


async def run(config):
    from aiokafka import AIOKafkaConsumer
    import aiomysql

    run_id = config.get("RUN_ID") or config.get("DEV_RUN_ID") or config.get("STAGING_RUN_ID")
    consumer = AIOKafkaConsumer(
        config.get("TOPIC") or topic_for(config["APP_ENVIRONMENT"]),
        bootstrap_servers=config["KAFKA_BOOTSTRAP_SERVERS"],
        group_id=run_id + "-python-metadata", auto_offset_reset="earliest",
        enable_auto_commit=True, client_id=run_id + "-python-metadata",
    )
    pool = await aiomysql.create_pool(
        host=config["MYSQL_HOST"], port=3306, user=config["MYSQL_USER"],
        password=config["MYSQL_PASSWORD"], db=config["MYSQL_DATABASE"],
        minsize=1, maxsize=2, connect_timeout=5, autocommit=True,
        charset="utf8mb4", init_command="SET time_zone='+00:00'",
    )
    projector = IncrementalTaskMetadataProjector()
    from ..runtime import ensure_database_identity
    try:
        await ensure_database_identity(pool, config)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT run_id,task_id,source_id,revision,event_count,changed_field_count,"
                    "created_count,saved_count,claimed_count,assigned_count,reviewed_count,"
                    "archived_count,deleted_count FROM dev_task_metadata_python WHERE run_id=%s",
                    (run_id,),
                )
                columns = ("run_id", "task_id", "source_id", "revision", "event_count", "changed_field_count",
                           "created_count", "saved_count", "claimed_count", "assigned_count", "reviewed_count",
                           "archived_count", "deleted_count")
                for row in await cur.fetchall():
                    persisted = dict(zip(columns, row))
                    persisted["environment"] = config["APP_ENVIRONMENT"]
                    projector.restore_snapshot(persisted)
        await consumer.start()
        async for message in consumer:
            event = validate_task_event(json.loads(message.value))
            if event["run_id"] != run_id or event["environment"] != config["APP_ENVIRONMENT"]:
                continue
            async with pool.acquire() as conn:
                await conn.begin()
                async with conn.cursor() as cur:
                    ledger_sql, ledger_params = event_ledger_sql(event)
                    await cur.execute(
                        "SELECT canonical_sha256 FROM dev_task_metadata_python_events "
                        "WHERE run_id=%s AND event_id=%s FOR UPDATE",
                        (event["run_id"], event["event_id"]),
                    )
                    existing = await cur.fetchone()
                    digest = ledger_params[-1]
                    if existing is not None:
                        if existing[0] != digest:
                            raise ValueError("Python event ledger metadata conflict")
                        await conn.rollback()
                        continue
                    await cur.execute(ledger_sql, ledger_params)
                    row = projector.apply(event)
                    sql, params = upsert_sql(row)
                    await cur.execute(sql, params)
                await conn.commit()
    finally:
        await consumer.stop()
        pool.close()
        await pool.wait_closed()


__all__ = ["TABLE", "event_ledger_sql", "run", "upsert_sql"]
