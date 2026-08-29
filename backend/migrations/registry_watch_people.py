"""人员标签历史回填工具。

默认只读；只有显式传入 ``migrate --apply`` 才会写入 RegistryData。
执行生产迁移前应完成 RegistryData 备份并安排维护窗口。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

import aiomysql

from config import settings
from services.registry_watch_backfill import backfill_watch_people, verify_watch_people_backfill


async def close_connection(conn) -> None:
    conn.close()
    wait_closed = getattr(conn, "wait_closed", None)
    if wait_closed is not None:
        result = wait_closed()
        if inspect.isawaitable(result):
            await result


async def open_connection():
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_REGISTRY_DB,
        autocommit=False,
        charset="utf8mb4",
    )


async def run_backfill(apply: bool, batch_size: int) -> dict:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            result = await backfill_watch_people(cur, apply=apply, batch_size=batch_size)
        if apply:
            await conn.commit()
        else:
            await conn.rollback()
        return {"command": "migrate", "apply": apply, **result}
    except Exception:
        await conn.rollback()
        raise
    finally:
        await close_connection(conn)


async def run_verify() -> dict:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            result = await verify_watch_people_backfill(cur)
        await conn.rollback()
        return {"command": "verify", **result}
    finally:
        await close_connection(conn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", help="只读统计待回填数量")
    migrate = sub.add_parser("migrate", help="预览或执行历史回填")
    migrate.add_argument("--apply", action="store_true", help="明确授权写入 RegistryData")
    migrate.add_argument("--batch-size", type=int, default=500)
    sub.add_parser("verify", help="只读核验标签与人员档案关联")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "measure":
        print(json.dumps(await run_backfill(False, 500), ensure_ascii=False, indent=2))
    elif args.command == "migrate":
        print(json.dumps(await run_backfill(bool(args.apply), args.batch_size), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(await run_verify(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
