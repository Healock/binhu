"""历史第一核查人责任回填工具。

生产环境必须按 ``measure -> migrate --apply -> verify`` 执行，并在写入前
备份 OnlineData 与 daily_report。工具只使用明确的分配/移交事件或最早日报
流水；没有可靠历史证据时保留问题项，绝不使用当前核查人猜测第一责任人。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

import aiomysql

from config import settings
from services.task_assignment_responsibility import (
    audit_missing_first_assignments,
    backfill_missing_first_assignments,
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


def _summary(command: str, rows: list[dict]) -> dict:
    reasons = sorted({str(item.get("reason") or "") for item in rows if item.get("reason")})
    return {
        "command": command,
        "missing_total": len(rows),
        "resolvable": sum(1 for item in rows if item.get("resolved")),
        "unresolved": sum(1 for item in rows if not item.get("resolved")),
        "problem_reasons": {
            reason: sum(1 for item in rows if item.get("reason") == reason)
            for reason in reasons
        },
        "by_parser_type": {
            parser_type: sum(1 for item in rows if item["parser_type"] == parser_type)
            for parser_type in sorted({item["parser_type"] for item in rows})
        },
    }


async def measure(command: str = "measure") -> dict:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            rows = await audit_missing_first_assignments(
                cur,
                daily_report_schema=settings.MYSQL_DAILY_REPORT_DB,
            )
        await conn.rollback()
        return _summary(command, rows)
    finally:
        await close_connection(conn)


async def migrate() -> dict:
    conn = await open_connection()
    try:
        return {
            "command": "migrate",
            **await backfill_missing_first_assignments(
                conn,
                daily_report_schema=settings.MYSQL_DAILY_REPORT_DB,
            ),
        }
    finally:
        await close_connection(conn)


async def verify() -> dict:
    return await measure("verify")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", help="只读统计缺少第一核查人责任记录的任务")
    migrate_parser = sub.add_parser("migrate", help="预览或执行历史责任回填")
    migrate_parser.add_argument("--apply", action="store_true", help="明确授权写入 OnlineData")
    sub.add_parser("verify", help="只读核验回填后仍未解决的问题")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "measure":
        result = await measure()
    elif args.command == "migrate" and args.apply:
        result = await migrate()
    elif args.command == "migrate":
        result = await measure("migrate-preview")
    else:
        result = await verify()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
