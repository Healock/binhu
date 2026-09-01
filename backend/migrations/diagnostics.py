"""显式创建、核验诊断任务表；默认只读。"""

from __future__ import annotations

import argparse
import asyncio
import json

import aiomysql

from config import settings
from services.diagnostics import ensure_diagnostic_schema_sql


async def run(command: str, apply: bool) -> dict:
    conn = await aiomysql.connect(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER, password=settings.MYSQL_PASSWORD, db=settings.MYSQL_PLATFORM_DB, autocommit=True, charset="utf8mb4")
    try:
        async with conn.cursor() as cur:
            if command == "migrate" and apply:
                await cur.execute("SET SESSION lock_wait_timeout=5")
                for statement in ensure_diagnostic_schema_sql():
                    await cur.execute(statement)
            tables = []
            for table in ("diagnostic_jobs", "diagnostic_reports"):
                await cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (settings.MYSQL_PLATFORM_DB, table))
                tables.append({"table": table, "exists": bool((await cur.fetchone())[0])})
            return {"command": command, "apply": apply, "database": settings.MYSQL_PLATFORM_DB, "tables": tables, "consistent": all(item["exists"] for item in tables)}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--apply", action="store_true")
    sub.add_parser("verify")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.command, getattr(args, "apply", False))), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
