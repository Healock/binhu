"""Create and verify transactional outbox tables.

The command is intentionally explicit: normal application startup creates the
compatible table lazily, while production DDL should be previewed and applied
from a maintenance window with database backups already complete.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

import aiomysql

from config import settings
from services.domain_events import ensure_outbox_schema_sql


async def close_connection(conn) -> None:
    conn.close()
    waiter = getattr(conn, "wait_closed", None)
    if waiter:
        result = waiter()
        if inspect.isawaitable(result):
            await result


async def open_connection(database: str):
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=database,
        autocommit=True,
        charset="utf8mb4",
    )


async def table_info(cur, database: str) -> dict:
    await cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name='_domain_event_outbox'",
        (database,),
    )
    exists = bool((await cur.fetchone())[0])
    if not exists:
        return {"database": database, "exists": False, "columns": []}
    await cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name='_domain_event_outbox' "
        "ORDER BY ordinal_position",
        (database,),
    )
    return {
        "database": database,
        "exists": True,
        "columns": [str(row[0]) for row in await cur.fetchall()],
    }


async def run(command: str, apply: bool) -> dict:
    databases = [settings.MYSQL_ONLINE_DATA_DB, settings.MYSQL_PLATFORM_DB]
    result = {"command": command, "apply": apply, "databases": []}
    for database in databases:
        conn = await open_connection(database)
        try:
            async with conn.cursor() as cur:
                if command == "measure":
                    result["databases"].append(await table_info(cur, database))
                elif command == "migrate":
                    if apply:
                        await cur.execute("SET SESSION lock_wait_timeout=5")
                        await cur.execute(ensure_outbox_schema_sql())
                    result["databases"].append(await table_info(cur, database))
                else:
                    info = await table_info(cur, database)
                    required = {
                        "event_id", "schema_version", "domain", "event_type",
                        "aggregate_type", "aggregate_id", "aggregate_revision",
                        "audiences_json", "status", "attempt_count", "available_at",
                        "locked_by", "locked_until", "last_error_code",
                        "last_error_summary", "occurred_at", "published_at",
                    }
                    info["consistent"] = info["exists"] and required.issubset(set(info["columns"]))
                    result["databases"].append(info)
        finally:
            await close_connection(conn)
    result["consistent"] = all(item.get("consistent", item.get("exists", False)) for item in result["databases"])
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--apply", action="store_true")
    sub.add_parser("verify")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    print(json.dumps(await run(args.command, getattr(args, "apply", False)), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
