"""本地来源重复审计与安全归并维护命令。

默认只读；生产执行 ``apply`` 前必须完成对应数据库备份并冻结来源写入。
"""

from __future__ import annotations

import argparse
import asyncio
import json

import aiomysql

from config import settings
from services.local_source import cleanup_duplicate_local_sources


async def main(mode: str) -> None:
    conn = await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_ONLINE_DATA_DB,
        autocommit=False,
        charset="utf8mb4",
    )
    try:
        result = await cleanup_duplicate_local_sources(conn, apply=mode == "apply")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        conn.close()
        wait_closed = getattr(conn, "wait_closed", None)
        if wait_closed:
            value = wait_closed()
            if asyncio.iscoroutine(value):
                await value


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("measure", "apply", "verify"), nargs="?", default="measure")
    args = parser.parse_args()
    asyncio.run(main("apply" if args.mode == "apply" else "measure"))
