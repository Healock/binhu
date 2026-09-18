"""Idempotently add Dev delivery claim indexes to an existing database."""
from __future__ import annotations

import asyncio
import os

import aiomysql


async def main() -> None:
    conn = await aiomysql.connect(
        host=os.environ["MYSQL_HOST"], port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"],
        db=os.environ["MYSQL_DATABASE"], autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT index_name FROM information_schema.statistics "
                "WHERE table_schema=DATABASE() AND table_name='_kafka_event_delivery'"
            )
            existing = {str(row[0]) for row in await cur.fetchall()}
            if "claim_pending" not in existing:
                await cur.execute(
                    "ALTER TABLE _kafka_event_delivery ADD INDEX claim_pending "
                    "(run_id,status,created_at,event_id,available_at)"
                )
    finally:
        conn.close()
        waiter = getattr(conn, "wait_closed", None)
        if waiter:
            result = waiter()
            if asyncio.iscoroutine(result):
                await result


if __name__ == "__main__":
    asyncio.run(main())
