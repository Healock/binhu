"""Idempotently prepare delivery indexes and retained Dev projection schemas."""
from __future__ import annotations

import asyncio
import os

import aiomysql


async def prepare_dev_projections(cur) -> None:
    from .projection_definitions import PROJECTIONS, projection_schema_statements

    await cur.execute("SELECT DATABASE(),environment,database_name FROM _pipeline_identity WHERE id=1")
    if await cur.fetchone() != ("Dev_EventPipeline", "development", "Dev_EventPipeline"):
        raise ValueError("dev_projection_database_identity_mismatch")
    for statement in projection_schema_statements(if_not_exists=True):
        await cur.execute(statement)
    for item in PROJECTIONS:
        columns = tuple(column.partition(" ")[0] for column in item.columns)
        await cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s ORDER BY ordinal_position",
            (item.name,),
        )
        if tuple(row[0] for row in await cur.fetchall()) != columns:
            raise ValueError("dev_projection_columns_mismatch")
        await cur.execute(
            "SELECT column_name FROM information_schema.statistics "
            "WHERE table_schema=DATABASE() AND table_name=%s AND index_name='PRIMARY' ORDER BY seq_in_index",
            (item.name,),
        )
        if tuple(row[0] for row in await cur.fetchall()) != item.primary_key:
            raise ValueError("dev_projection_primary_key_mismatch")


async def main() -> None:
    development = os.environ.get("APP_ENVIRONMENT") == "development"
    if development and (os.environ.get("MYSQL_HOST"), os.environ.get("MYSQL_DATABASE"),
                        os.environ.get("MYSQL_USER")) != ("dev-derived-mysql", "Dev_EventPipeline", "dev_pipeline"):
        raise ValueError("dev_projection_database_identity_mismatch")
    conn = await aiomysql.connect(
        host=os.environ["MYSQL_HOST"], port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"],
        db=os.environ["MYSQL_DATABASE"], autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            if development:
                await cur.execute("SET SESSION lock_wait_timeout=5")
                await prepare_dev_projections(cur)
            await cur.execute(
                "SELECT index_name FROM information_schema.statistics "
                "WHERE table_schema=DATABASE() AND table_name='_kafka_event_delivery'"
            )
            existing = {str(row[0]) for row in await cur.fetchall()}
            await cur.execute(
                "SELECT column_name FROM information_schema.statistics "
                "WHERE table_schema=DATABASE() AND table_name='_kafka_event_delivery' "
                "AND index_name='PRIMARY' ORDER BY seq_in_index"
            )
            primary_columns = [str(row[0]) for row in await cur.fetchall()]
            if primary_columns == ["event_id"]:
                await cur.execute(
                    "ALTER TABLE _kafka_event_delivery DROP PRIMARY KEY, "
                    "ADD PRIMARY KEY (run_id,event_id)"
                )
            elif primary_columns not in ([], ["run_id", "event_id"]):
                raise RuntimeError("delivery_primary_key_contract_mismatch")
            if "claim_pending" not in existing:
                await cur.execute(
                    "ALTER TABLE _kafka_event_delivery ADD INDEX claim_pending "
                    "(run_id,status,created_at,event_id,available_at)"
                )
            if "published_retention" not in existing:
                await cur.execute(
                    "ALTER TABLE _kafka_event_delivery ADD INDEX published_retention "
                    "(run_id,status,published_at,event_id)"
                )
            await cur.execute(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema=DATABASE() AND table_name='_kafka_event_delivery' "
                "AND index_name='published_retention'"
            )
            if not (await cur.fetchone())[0]:
                raise RuntimeError("delivery_retention_index_missing")
            await cur.execute(
                "SELECT column_name FROM information_schema.statistics "
                "WHERE table_schema=DATABASE() AND table_name='_kafka_event_delivery' "
                "AND index_name='PRIMARY' ORDER BY seq_in_index"
            )
            if [str(row[0]) for row in await cur.fetchall()] != ["run_id", "event_id"]:
                raise RuntimeError("delivery_primary_key_missing")
    finally:
        conn.close()
        waiter = getattr(conn, "wait_closed", None)
        if waiter:
            result = waiter()
            if asyncio.iscoroutine(result):
                await result


if __name__ == "__main__":
    asyncio.run(main())
