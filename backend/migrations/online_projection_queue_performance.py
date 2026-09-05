"""0.28.8 派生队列调度列与本地来源索引维护工具。

生产固定执行 ``measure -> migrate --apply -> verify``。默认命令只读取
information_schema、队列统计和 EXPLAIN；只有显式 ``migrate --apply`` 才会
增加调度列、回填可执行时间并创建索引。历史列和历史索引不会在本版本删除。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from typing import Any

import aiomysql

from config import settings


QUEUE_TABLE = "_online_projection_jobs"
SOURCE_TABLE = "_online_source_rows"
QUEUE_INDEX = "idx_projection_job_available"
SOURCE_INDEX = "idx_online_source_ref"


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


async def _column_exists(cur, table: str, column: str) -> bool:
    await cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
        (table, column),
    )
    row = await cur.fetchone()
    return bool(row and int(row[0] or 0))


async def _column_definition(cur, table: str, column: str) -> tuple[str, Any] | None:
    await cur.execute(
        "SELECT is_nullable,column_default FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
        (table, column),
    )
    row = await cur.fetchone()
    return (str(row[0]), row[1]) if row else None


async def _index_columns(cur, table: str) -> dict[str, list[str]]:
    await cur.execute(
        "SELECT index_name,column_name FROM information_schema.statistics "
        "WHERE table_schema=DATABASE() AND table_name=%s "
        "ORDER BY index_name,seq_in_index",
        (table,),
    )
    result: dict[str, list[str]] = {}
    for name, column in await cur.fetchall():
        result.setdefault(str(name), []).append(str(column))
    return result


async def _table_exists(cur, table: str) -> bool:
    await cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name=%s",
        (table,),
    )
    row = await cur.fetchone()
    return bool(row and int(row[0] or 0))


async def _explain(cur, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    await cur.execute("EXPLAIN " + sql, params)
    columns = [str(item[0]) for item in (cur.description or [])]
    return [dict(zip(columns, row, strict=False)) for row in await cur.fetchall()]


async def _table_sizes(cur) -> dict[str, dict[str, int]]:
    await cur.execute(
        "SELECT table_name,table_rows,data_length,index_length "
        "FROM information_schema.tables WHERE table_schema=DATABASE() "
        "AND table_name IN (%s,%s)",
        (QUEUE_TABLE, SOURCE_TABLE),
    )
    return {
        str(table): {
            "estimated_rows": int(rows or 0),
            "data_bytes": int(data_bytes or 0),
            "index_bytes": int(index_bytes or 0),
        }
        for table, rows, data_bytes, index_bytes in await cur.fetchall()
    }


async def summarize(cur, command: str) -> dict[str, Any]:
    required = {table: await _table_exists(cur, table) for table in (QUEUE_TABLE, SOURCE_TABLE)}
    if not all(required.values()):
        return {"command": command, "tables": required, "ready": False}
    available_at = await _column_exists(cur, QUEUE_TABLE, "available_at")
    queue_indexes = await _index_columns(cur, QUEUE_TABLE)
    source_indexes = await _index_columns(cur, SOURCE_TABLE)
    await cur.execute(
        "SELECT COUNT(*),"
        "SUM(status IN ('pending','retry')),"
        "SUM(status='running'),"
        "SUM(status='failed') FROM _online_projection_jobs"
    )
    queue_row = await cur.fetchone() or (0, 0, 0, 0)
    null_available = None
    if available_at:
        await cur.execute(
            "SELECT COUNT(*) FROM _online_projection_jobs WHERE available_at IS NULL"
        )
        null_available = int((await cur.fetchone())[0] or 0)
    await cur.execute(
        "SELECT COUNT(*) FROM ("
        "SELECT source_kind,source_ref FROM _online_source_rows "
        "WHERE source_kind<>'' AND source_ref<>'' AND archived_at IS NULL "
        "GROUP BY source_kind,source_ref HAVING COUNT(*)>1"
        ") duplicate_refs"
    )
    duplicate_refs = int((await cur.fetchone())[0] or 0)
    due_column = "available_at" if available_at else "next_attempt_at"
    due_condition = (
        "available_at<=UTC_TIMESTAMP()"
        if available_at
        else "(next_attempt_at IS NULL OR next_attempt_at<=UTC_TIMESTAMP())"
    )
    query_plans = {
        "queue_claim": await _explain(
            cur,
            "SELECT id FROM _online_projection_jobs "
            f"WHERE status IN ('pending','retry') AND {due_condition} "
            "ORDER BY created_at,id LIMIT 100",
        ),
        "source_lookup": await _explain(
            cur,
            "SELECT id FROM _online_source_rows "
            "WHERE source_kind=%s AND source_ref=%s LIMIT 1",
            ("local", "measure-placeholder"),
        ),
    }
    return {
        "command": command,
        "tables": required,
        "ready": bool(
            available_at
            and queue_indexes.get(QUEUE_INDEX) == ["status", "available_at", "created_at", "id"]
            and source_indexes.get(SOURCE_INDEX) == ["source_kind", "source_ref"]
            and not null_available
        ),
        "available_at": available_at,
        "available_at_null_rows": null_available,
        "queue_index": queue_indexes.get(QUEUE_INDEX),
        "source_index": source_indexes.get(SOURCE_INDEX),
        "active_duplicate_source_refs": duplicate_refs,
        "queue_due_column": due_column,
        "table_sizes": await _table_sizes(cur),
        "query_plans": query_plans,
        "queue": {
            "total": int(queue_row[0] or 0),
            "queued": int(queue_row[1] or 0),
            "running": int(queue_row[2] or 0),
            "failed": int(queue_row[3] or 0),
        },
        "rollback_sql": [
            f"ALTER TABLE {QUEUE_TABLE} DROP INDEX {QUEUE_INDEX}",
            f"ALTER TABLE {SOURCE_TABLE} DROP INDEX {SOURCE_INDEX}",
            f"ALTER TABLE {QUEUE_TABLE} DROP COLUMN available_at",
        ],
    }


async def migrate() -> dict[str, Any]:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            before = await summarize(cur, "before")
            if not all(before.get("tables", {}).values()):
                raise RuntimeError("OnlineData 缺少派生队列或来源表")
            if not await _column_exists(cur, QUEUE_TABLE, "available_at"):
                await cur.execute(
                    "ALTER TABLE _online_projection_jobs "
                    "ADD COLUMN available_at DATETIME NULL, "
                    "ALGORITHM=INSTANT"
                )
            await cur.execute(
                "UPDATE _online_projection_jobs "
                "SET available_at=COALESCE(next_attempt_at,created_at,UTC_TIMESTAMP()) "
                "WHERE available_at IS NULL"
            )
            definition = await _column_definition(cur, QUEUE_TABLE, "available_at")
            if not definition or definition[0].upper() != "NO" or not definition[1]:
                await cur.execute(
                    "ALTER TABLE _online_projection_jobs "
                    "MODIFY COLUMN available_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "ALGORITHM=INPLACE, LOCK=NONE"
                )
            queue_indexes = await _index_columns(cur, QUEUE_TABLE)
            if QUEUE_INDEX not in queue_indexes:
                await cur.execute(
                    "ALTER TABLE _online_projection_jobs "
                    "ADD INDEX idx_projection_job_available "
                    "(status,available_at,created_at,id), ALGORITHM=INPLACE, LOCK=NONE"
                )
            source_indexes = await _index_columns(cur, SOURCE_TABLE)
            if SOURCE_INDEX not in source_indexes:
                await cur.execute(
                    "ALTER TABLE _online_source_rows "
                    "ADD INDEX idx_online_source_ref (source_kind,source_ref), "
                    "ALGORITHM=INPLACE, LOCK=NONE"
                )
            after = await summarize(cur, "after")
        await conn.commit()
        return {"command": "migrate", "before": before, "after": after}
    except Exception:
        await conn.rollback()
        raise
    finally:
        await close_connection(conn)


async def measure(command: str) -> dict[str, Any]:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            result = await summarize(cur, command)
        await conn.rollback()
        return result
    finally:
        await close_connection(conn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", help="只读统计队列结构、索引、积压和重复来源")
    migrate_parser = sub.add_parser("migrate", help="预览或执行调度列回填及索引创建")
    migrate_parser.add_argument("--apply", action="store_true", help="明确授权修改 OnlineData")
    sub.add_parser("verify", help="只读核验调度列、索引和空值")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "migrate" and args.apply:
        result = await migrate()
    elif args.command == "migrate":
        result = await measure("migrate-preview")
    else:
        result = await measure(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
