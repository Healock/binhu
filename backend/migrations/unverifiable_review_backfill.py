"""无法核实任务结构化研判流程接管工具。

生产环境必须按 ``measure -> migrate --apply -> verify`` 执行；
写入前备份 OnlineData，并确保只有当前有效的 canonical 本地来源参与判定。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

import aiomysql

from config import settings
from services.unverifiable_review import (
    SOURCE_EXCEPTION,
    UNVERIFIABLE_REVIEW_TYPES,
    audit_missing_unverifiable_flows,
    backfill_missing_unverifiable_flows,
)


async def open_connection():
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_ONLINE_DATA_DB,
        autocommit=False,
        charset="utf8mb4",
    )


async def close_connection(conn) -> None:
    conn.close()
    wait_closed = getattr(conn, "wait_closed", None)
    if wait_closed:
        result = wait_closed()
        if inspect.isawaitable(result):
            await result


async def measure() -> dict:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            rows = await audit_missing_unverifiable_flows(cur)
        await conn.rollback()
        return {
            "command": "measure",
            "missing_total": len(rows),
            "initial_pending": sum(1 for item in rows if item["source_count"] == 1 and not item["conflict"]),
            "source_exception": sum(1 for item in rows if item["source_count"] != 1 or item["conflict"]),
            "by_parser_type": {
                parser_type: sum(1 for item in rows if item["parser_type"] == parser_type)
                for parser_type in UNVERIFIABLE_REVIEW_TYPES
            },
        }
    finally:
        await close_connection(conn)


async def verify() -> dict:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            missing = await audit_missing_unverifiable_flows(cur)
            placeholders = ",".join(["%s"] * len(UNVERIFIABLE_REVIEW_TYPES))
            await cur.execute(
                f"SELECT state,COUNT(*) FROM _unverifiable_review_flows "
                f"WHERE parser_type IN ({placeholders}) GROUP BY state",
                UNVERIFIABLE_REVIEW_TYPES,
            )
            states = {str(state): int(count) for state, count in await cur.fetchall()}
        await conn.rollback()
        return {
            "command": "verify",
            "missing_total": len(missing),
            "source_exception": states.get(SOURCE_EXCEPTION, 0),
            "flow_states": states,
        }
    finally:
        await close_connection(conn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", help="只读统计尚未接管的无法核实任务")
    migrate = sub.add_parser("migrate", help="预览或执行流程接管")
    migrate.add_argument("--apply", action="store_true", help="明确授权写入 OnlineData")
    sub.add_parser("verify", help="只读核验接管结果")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "measure" or (args.command == "migrate" and not args.apply):
        result = await measure()
        if args.command == "migrate":
            result["command"] = "migrate-preview"
    elif args.command == "migrate":
        conn = await open_connection()
        try:
            result = {"command": "migrate", **await backfill_missing_unverifiable_flows(conn)}
        finally:
            await close_connection(conn)
    else:
        result = await verify()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
