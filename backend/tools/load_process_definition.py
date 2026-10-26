"""Load one declarative process JSON into WorkflowData idempotently."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import aiomysql

from config import settings
from services.process_definition_loader import load_process_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path, help="JSON 流程定义文件")
    parser.add_argument("--created-by", type=int, default=None, help="审计用管理员账号 ID")
    return parser


async def run(args: argparse.Namespace) -> dict:
    bundle = json.loads(args.file.read_text(encoding="utf-8"))
    conn = await aiomysql.connect(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD, db=settings.MYSQL_WORKFLOW_DB,
        autocommit=False, charset="utf8mb4",
    )
    try:
        async with conn.cursor() as cur:
            result = await load_process_bundle(cur, bundle, actor_user_id=args.created_by)
        await conn.commit()
        return result
    except BaseException:
        await conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(build_parser().parse_args())), ensure_ascii=False, indent=2))
