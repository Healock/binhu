"""房屋档案与小区地址库匹配维护工具。

默认只读；只有显式传入 ``migrate --apply`` 才会写入 RegistryData。
生产执行前必须备份 RegistryData，OnlineData 仅作为只读小区地址库来源。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

import aiomysql

from config import settings
from services.property_small_community_matching import (
    load_address_entries,
    run_property_matching,
    verify_property_matching,
)


async def close_connection(conn) -> None:
    conn.close()
    wait_closed = getattr(conn, "wait_closed", None)
    if wait_closed is not None:
        result = wait_closed()
        if inspect.isawaitable(result):
            await result


async def open_connection(database: str):
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=database,
        autocommit=False,
        charset="utf8mb4",
    )


async def run_matching(*, apply: bool) -> dict:
    online_conn = await open_connection(settings.MYSQL_ONLINE_DATA_DB)
    registry_conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    try:
        async with online_conn.cursor() as online_cur:
            address_entries = await load_address_entries(online_cur)
        await online_conn.rollback()
        async with registry_conn.cursor() as registry_cur:
            result = await run_property_matching(
                registry_cur,
                address_entries,
                apply=apply,
            )
        if apply:
            await registry_conn.commit()
        else:
            await registry_conn.rollback()
        return {
            "command": "migrate" if apply else "measure",
            "address_entry_count": len(address_entries),
            **result,
        }
    except Exception:
        await registry_conn.rollback()
        raise
    finally:
        await close_connection(registry_conn)
        await close_connection(online_conn)


async def run_verify() -> dict:
    registry_conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    try:
        async with registry_conn.cursor() as registry_cur:
            result = await verify_property_matching(registry_cur)
        await registry_conn.rollback()
        return {"command": "verify", **result}
    finally:
        await close_connection(registry_conn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", help="只读统计房屋匹配建议")
    migrate = sub.add_parser("migrate", help="预览或写入房屋小区匹配建议")
    migrate.add_argument("--apply", action="store_true", help="明确授权写入 RegistryData")
    sub.add_parser("verify", help="只读核验房屋小区关联状态")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "measure":
        result = await run_matching(apply=False)
    elif args.command == "migrate":
        if not args.apply:
            result = await run_matching(apply=False)
            result["command"] = "migrate-preview"
        else:
            result = await run_matching(apply=True)
    else:
        result = await run_verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
