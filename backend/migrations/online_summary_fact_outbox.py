"""Provision the durable daily-report online-summary fact outbox."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import re

import aiomysql

from config import settings
from services.online_summary_updates import online_summary_fact_outbox_schema_sql


TABLE = "_online_summary_fact_outbox"
REQUIRED_COLUMNS = {
    "event_id", "business_date", "parser_type", "row_key_hmac", "task_id",
    "source_id", "task_revision", "status", "error_code", "fact_json", "attempt_count",
    "available_at", "locked_by", "locked_until", "created_at", "published_at",
}
REQUIRED_INDEXES = {
    "PRIMARY", "uk_online_summary_fact_revision", "idx_online_summary_fact_due",
    "idx_online_summary_fact_expired",
}

MISSING_COLUMN_DDL = {
    "attempt_count": "ALTER TABLE _online_summary_fact_outbox ADD COLUMN attempt_count INT UNSIGNED NOT NULL DEFAULT 0",
    "available_at": "ALTER TABLE _online_summary_fact_outbox ADD COLUMN available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)",
    "locked_by": "ALTER TABLE _online_summary_fact_outbox ADD COLUMN locked_by VARCHAR(96) NULL",
    "locked_until": "ALTER TABLE _online_summary_fact_outbox ADD COLUMN locked_until DATETIME(6) NULL",
    "published_at": "ALTER TABLE _online_summary_fact_outbox ADD COLUMN published_at DATETIME(6) NULL",
}
MISSING_INDEX_DDL = {
    "idx_online_summary_fact_due": (
        "ALTER TABLE _online_summary_fact_outbox ADD INDEX idx_online_summary_fact_due "
        "(status,available_at,created_at,event_id)"
    ),
    "idx_online_summary_fact_expired": (
        "ALTER TABLE _online_summary_fact_outbox ADD INDEX idx_online_summary_fact_expired "
        "(status,locked_until,created_at,event_id)"
    ),
}


def schema_status(columns: set[str], indexes: set[str]) -> dict:
    missing_columns = sorted(REQUIRED_COLUMNS - columns)
    missing_indexes = sorted(REQUIRED_INDEXES - indexes)
    return {
        "table": TABLE,
        "columns": sorted(columns),
        "indexes": sorted(indexes),
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "consistent": not missing_columns and not missing_indexes,
    }


def validate_target(environment: str, database: str) -> None:
    prefixes = {"development": "Dev_", "staging": "Staging_"}
    prefix = prefixes.get(environment)
    if prefix is None or re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_]+", database) is None:
        raise ValueError("non-Production online summary database required")


async def _connect():
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_DAILY_REPORT_DB,
        autocommit=True,
        charset="utf8mb4",
        connect_timeout=5,
    )


async def _close(conn) -> None:
    conn.close()
    waiter = getattr(conn, "wait_closed", None)
    if waiter:
        result = waiter()
        if inspect.isawaitable(result):
            await result


async def inspect_schema(cur) -> dict:
    await cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name=%s ORDER BY ordinal_position",
        (TABLE,),
    )
    columns = {str(row[0]) for row in await cur.fetchall()}
    await cur.execute(
        "SELECT index_name FROM information_schema.statistics "
        "WHERE table_schema=DATABASE() AND table_name=%s",
        (TABLE,),
    )
    indexes = {str(row[0]) for row in await cur.fetchall()}
    return {
        "database": settings.MYSQL_DAILY_REPORT_DB,
        **schema_status(columns, indexes),
    }


async def run(command: str, apply: bool = False) -> dict:
    validate_target(settings.APP_ENVIRONMENT, settings.MYSQL_DAILY_REPORT_DB)
    conn = await _connect()
    try:
        async with conn.cursor() as cur:
            if command == "migrate" and apply:
                await cur.execute("SET SESSION lock_wait_timeout=5")
                await cur.execute(online_summary_fact_outbox_schema_sql())
                current = await inspect_schema(cur)
                for column in current["missing_columns"]:
                    ddl = MISSING_COLUMN_DDL.get(column)
                    if ddl:
                        await cur.execute(ddl)
                current = await inspect_schema(cur)
                for index in current["missing_indexes"]:
                    ddl = MISSING_INDEX_DDL.get(index)
                    if ddl:
                        await cur.execute(ddl)
            result = await inspect_schema(cur)
            return {"command": command, "apply": apply, **result}
    finally:
        await _close(conn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--apply", action="store_true")
    sub.add_parser("verify")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    result = await run(args.command, getattr(args, "apply", False))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "verify" and not result["consistent"]:
        raise SystemExit("online_summary_fact_outbox_inconsistent")


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
