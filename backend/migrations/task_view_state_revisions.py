"""Provision the transactional revision ledger for task view-state events."""
from __future__ import annotations

import argparse
import asyncio
import json

import aiomysql

from config import settings
from services.task_view_state import TASK_VIEW_STATE_REVISIONS_SQL


REQUIRED_COLUMNS = {
    "source_id", "domain", "domain_revision", "task_revision", "updated_at",
    "state_fingerprint",
}


async def inspect(cur) -> dict:
    await cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name='_task_view_state_revisions' "
        "ORDER BY ordinal_position"
    )
    columns = {str(row[0]) for row in await cur.fetchall()}
    await cur.execute(
        "SELECT index_name FROM information_schema.statistics "
        "WHERE table_schema=DATABASE() AND table_name='_task_view_state_revisions'"
    )
    indexes = {str(row[0]) for row in await cur.fetchall()}
    return {
        "database": settings.MYSQL_ONLINE_DATA_DB,
        "columns": sorted(columns),
        "missing_columns": sorted(REQUIRED_COLUMNS - columns),
        "primary_key_present": "PRIMARY" in indexes,
    }


async def run(command: str, apply: bool = False) -> dict:
    if settings.APP_ENVIRONMENT not in {"development", "staging"}:
        raise ValueError("isolated environment required")
    conn = await aiomysql.connect(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER, password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_ONLINE_DATA_DB, connect_timeout=5,
        autocommit=True, charset="utf8mb4",
    )
    try:
        async with conn.cursor() as cur:
            before = await inspect(cur)
            if command == "migrate" and apply and before["missing_columns"]:
                await cur.execute("SET SESSION lock_wait_timeout=5")
                await cur.execute(TASK_VIEW_STATE_REVISIONS_SQL)
            result = await inspect(cur)
            result.update({
                "command": command,
                "apply": apply,
                "consistent": not result["missing_columns"] and result["primary_key_present"],
            })
            return result
    finally:
        conn.close()
        await conn.wait_closed()


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
        raise SystemExit("task_view_state_revision_schema_inconsistent")


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
