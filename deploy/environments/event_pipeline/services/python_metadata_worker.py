"""Dev-only Python implementation of the task metadata projection."""
from __future__ import annotations

import json
from typing import Any

from .kafka_event_contract import validate_task_event
from .task_metadata_projection import flatten_projection, project_events

TABLE = "dev_task_metadata_python"


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

    consumer = AIOKafkaConsumer(
        "dev.task.events.v1", bootstrap_servers=config["KAFKA_BOOTSTRAP_SERVERS"],
        group_id=config["DEV_RUN_ID"] + "-python-metadata", auto_offset_reset="earliest",
        enable_auto_commit=True, client_id=config["DEV_RUN_ID"] + "-python-metadata",
    )
    pool = await aiomysql.create_pool(
        host=config["MYSQL_HOST"], port=3306, user=config["MYSQL_USER"],
        password=config["MYSQL_PASSWORD"], db=config["MYSQL_DATABASE"],
        minsize=1, maxsize=2, connect_timeout=5, autocommit=True,
        charset="utf8mb4", init_command="SET time_zone='+00:00'",
    )
    events: list[dict[str, Any]] = []
    await consumer.start()
    try:
        async for message in consumer:
            event = validate_task_event(json.loads(message.value))
            if event["run_id"] != config["DEV_RUN_ID"]:
                continue
            events.append(event)
            row = project_events(events)[(event["run_id"], event["task_id"], event["source_id"])]
            sql, params = upsert_sql(row)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
    finally:
        await consumer.stop()
        pool.close()
        await pool.wait_closed()


__all__ = ["TABLE", "run", "upsert_sql"]
