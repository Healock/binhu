"""0.16.0 候选索引测量与可回退创建工具。

默认只输出已有索引、左前缀重复判断和建议 SQL。只有在生产只读 EXPLAIN
证明扫描行数明显下降后，才可在相应维护窗口使用 ``--apply``。每个新增索引
都有对应 DROP 语句，不会删除旧索引。
"""

from __future__ import annotations

import argparse
import asyncio
import json

import aiomysql

from config import settings
from migrations.domain_migration import quote_identifier


CANDIDATES = (
    (settings.MYSQL_ONLINE_DATA_DB, "_online_source_projection", "idx_projection_scope_state", ("parser_type", "community", "inspector", "task_state")),
    (settings.MYSQL_ONLINE_DATA_DB, "_online_source_projection", "idx_projection_state_updated", ("parser_type", "task_state", "updated_at")),
    (settings.MYSQL_DAILY_REPORT_DB, "_daily_task_ledger", "idx_ledger_report_scope", ("report_date", "community", "inspector")),
    (settings.MYSQL_DAILY_REPORT_DB, "_daily_task_ledger", "idx_ledger_parser_state", ("parser_type", "report_date", "task_state")),
    (settings.MYSQL_VISIT_DB, "t_visit_details", "idx_visit_date_community_operator", ("业务日期", "社区", "网格员姓名")),
    (settings.MYSQL_DISPATCH_DB, "_police_dispatch_tasks", "idx_dispatch_batch_community_publish", ("batch_id", "final_community_id", "publish_status")),
)


async def connection():
    return await aiomysql.connect(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD, db=settings.MYSQL_ONLINE_DATA_DB,
        autocommit=True, charset="utf8mb4",
    )


async def existing_indexes(cur, schema: str, table: str) -> dict[str, tuple[str, ...]]:
    await cur.execute(
        "SELECT index_name, column_name FROM information_schema.statistics "
        "WHERE table_schema=%s AND table_name=%s ORDER BY index_name, seq_in_index",
        (schema, table),
    )
    result: dict[str, list[str]] = {}
    for name, column in await cur.fetchall():
        result.setdefault(str(name), []).append(str(column))
    return {name: tuple(columns) for name, columns in result.items()}


def has_left_prefix(indexes: dict[str, tuple[str, ...]], columns: tuple[str, ...]) -> bool:
    return any(existing[: len(columns)] == columns for existing in indexes.values())


async def plan(apply: bool) -> list[dict]:
    conn = await connection()
    try:
        async with conn.cursor() as cur:
            result = []
            for schema, table, name, columns in CANDIDATES:
                await cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
                    (schema, table),
                )
                if not await cur.fetchone():
                    result.append({"schema": schema, "table": table, "index": name, "status": "table_missing"})
                    continue
                indexes = await existing_indexes(cur, schema, table)
                duplicate = name in indexes or has_left_prefix(indexes, columns)
                column_sql = ", ".join(quote_identifier(column) for column in columns)
                add_sql = (
                    f"ALTER TABLE {quote_identifier(schema)}.{quote_identifier(table)} "
                    f"ADD INDEX {quote_identifier(name)} ({column_sql}), ALGORITHM=INPLACE, LOCK=NONE"
                )
                drop_sql = (
                    f"ALTER TABLE {quote_identifier(schema)}.{quote_identifier(table)} "
                    f"DROP INDEX {quote_identifier(name)}, ALGORITHM=INPLACE, LOCK=NONE"
                )
                status = "duplicate_left_prefix" if duplicate else "recommended"
                if apply and not duplicate:
                    await cur.execute(add_sql)
                    status = "created"
                result.append({
                    "schema": schema, "table": table, "index": name, "columns": columns,
                    "status": status, "existing_indexes": indexes, "apply_sql": add_sql, "rollback_sql": drop_sql,
                })
            return result
    finally:
        conn.close()
        await conn.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(plan(args.apply)), ensure_ascii=False, indent=2))
