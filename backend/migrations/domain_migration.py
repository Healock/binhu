"""分域迁移的只读测量、幂等复制和切换核验工具。

默认只做 dry-run。生产维护窗口必须显式传 ``--apply``，并且由发布人先
冻结对应业务写入、完成备份后执行。旧表永不删除；切换开关由应用配置控制，
本工具只写入迁移状态表供运维核验。

示例（在后端容器或具备依赖的运维环境执行）：

    python -m migrations.domain_migration measure --schema OnlineData
    python -m migrations.domain_migration migrate --domain visit --apply
    python -m migrations.domain_migration verify --domain visit

本机没有真实 MySQL 时不要执行 ``--apply``。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
from typing import Any

import aiomysql

from config import settings


DOMAIN_TABLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "visit": (
        settings.MYSQL_ONLINE_DATA_DB,
        settings.MYSQL_VISIT_DB,
        ("_visit_import_batches", "t_visit_details", "_visit_import_issues"),
    ),
    "dispatch": (
        settings.MYSQL_ONLINE_DATA_DB,
        settings.MYSQL_DISPATCH_DB,
        ("_police_dispatch_batches", "_police_dispatch_tasks", "_police_dispatch_publish_results"),
    ),
    "platform": (
        settings.MYSQL_ONLINE_DATA_DB,
        settings.MYSQL_PLATFORM_DB,
        (
            "_users", "_sessions", "_grid_members", "_departments", "_community_aliases", "_communities",
            "_areas", "_area_leader_links", "_grid_member_department_links", "_permission_groups",
            "_position_permission_groups", "_position_permission_group_links", "_user_permission_group_links",
            "_permission_change_log", "_notifications", "_announcements", "_announcement_reads",
            "_admin_audit_log", "_personnel_attendance_history", "_personnel_weekend_duty", "_system_config",
            "_backup_schedule", "_backup_jobs", "_work_activity_events",
        ),
    ),
    "work_logs": (
        settings.MYSQL_ONLINE_DATA_DB,
        settings.MYSQL_DAILY_REPORT_DB,
        ("_work_log_drafts",),
    ),
    "registry_addresses": (
        settings.MYSQL_ONLINE_DATA_DB,
        settings.MYSQL_REGISTRY_DB,
        ("_police_address_entries", "_police_address_sources", "_police_address_imports", "_police_address_import_conflicts"),
    ),
}

def quote_identifier(value: str) -> str:
    # MySQL business tables contain Chinese column names. Permit Unicode
    # letters/digits while retaining the conservative punctuation policy.
    if not value or any(not (char.isalnum() or char in "_$") for char in value):
        raise ValueError(f"unsafe identifier: {value!r}")
    return f"`{value}`"


async def close_connection(conn) -> None:
    """Close an aiomysql connection across supported client versions."""
    conn.close()
    wait_closed = getattr(conn, "wait_closed", None)
    if wait_closed is None:
        return
    result = wait_closed()
    if inspect.isawaitable(result):
        await result


async def open_connection():
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_ONLINE_DATA_DB,
        autocommit=True,
        charset="utf8mb4",
    )


async def table_exists(cur, schema: str, table: str) -> bool:
    await cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return bool(await cur.fetchone())


async def primary_key_columns(cur, schema: str, table: str) -> list[str]:
    await cur.execute(
        "SELECT column_name FROM information_schema.key_column_usage "
        "WHERE table_schema=%s AND table_name=%s AND constraint_name='PRIMARY' "
        "ORDER BY ordinal_position",
        (schema, table),
    )
    return [str(row[0]) for row in await cur.fetchall()]


async def primary_key_column(cur, schema: str, table: str) -> str | None:
    columns = await primary_key_columns(cur, schema, table)
    return columns[0] if len(columns) == 1 else None


async def measure_schema(cur, schema: str) -> dict[str, Any]:
    await cur.execute(
        "SELECT table_name, table_rows, data_length, index_length, create_time, update_time "
        "FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name",
        (schema,),
    )
    rows = await cur.fetchall()
    tables: list[dict[str, Any]] = []
    for row in rows:
        table = str(row[0])
        primary_columns = await primary_key_columns(cur, schema, table)
        primary = primary_columns[0] if len(primary_columns) == 1 else None
        min_value = max_value = None
        if primary:
            await cur.execute(
                f"SELECT MIN({quote_identifier(primary)}), MAX({quote_identifier(primary)}) "
                f"FROM {quote_identifier(schema)}.{quote_identifier(table)}"
            )
            bounds = await cur.fetchone()
            min_value, max_value = bounds if bounds else (None, None)
        tables.append({
            "table": table,
            "rows_estimate": int(row[1] or 0),
            "data_bytes": int(row[2] or 0),
            "index_bytes": int(row[3] or 0),
            "primary_key": primary,
            "primary_key_columns": primary_columns,
            "min_primary": min_value,
            "max_primary": max_value,
            "created_at": row[4].isoformat() if row[4] else None,
            "updated_at": row[5].isoformat() if row[5] else None,
        })
    return {"schema": schema, "tables": tables}


async def copy_table(cur, source_schema: str, target_schema: str, table: str, chunk_size: int) -> dict[str, Any]:
    source = f"{quote_identifier(source_schema)}.{quote_identifier(table)}"
    target = f"{quote_identifier(target_schema)}.{quote_identifier(table)}"
    if not await table_exists(cur, source_schema, table):
        return {"table": table, "status": "source_missing", "copied": 0}
    structure = await ensure_target_structure(cur, source_schema, target_schema, table)
    primary_columns = await primary_key_columns(cur, source_schema, table)
    primary = primary_columns[0] if len(primary_columns) == 1 else None
    if not primary:
        # Tables without a primary key, or with a composite primary key, are
        # copied in one server-side statement. Paging only by the first part
        # of a composite key can skip rows that share that value.
        await cur.execute(f"INSERT IGNORE INTO {target} SELECT * FROM {source}")
    else:
        column_names = await _column_names(cur, source_schema, table)
        columns_sql = ", ".join(quote_identifier(column) for column in column_names)
        pk_index = column_names.index(primary)
        last = None
        copied = 0
        while True:
            if last is None:
                await cur.execute(
                    f"SELECT {columns_sql} FROM {source} ORDER BY {quote_identifier(primary)} LIMIT %s",
                    (chunk_size,),
                )
            else:
                await cur.execute(
                    f"SELECT {columns_sql} FROM {source} WHERE {quote_identifier(primary)}>%s "
                    f"ORDER BY {quote_identifier(primary)} LIMIT %s",
                    (last, chunk_size),
                )
            rows = await cur.fetchall()
            if not rows:
                break
            placeholders = ",".join(["%s"] * len(rows[0]))
            await cur.executemany(
                f"INSERT IGNORE INTO {target} ({columns_sql}) VALUES ({placeholders})",
                rows,
            )
            copied += len(rows)
            last = rows[-1][pk_index]
    await cur.execute(f"SELECT COUNT(*) FROM {source}")
    source_count = int((await cur.fetchone())[0])
    await cur.execute(f"SELECT COUNT(*) FROM {target}")
    target_count = int((await cur.fetchone())[0])
    return {"table": table, "status": "copied", "copied": copied if primary else source_count,
            "source_count": source_count, "target_count": target_count,
            "structure_consistent": structure["structure_consistent"],
            "consistent": source_count == target_count and structure["structure_consistent"]}


async def _columns_sql(cur, schema: str, table: str) -> str:
    columns = await _column_names(cur, schema, table)
    if not columns:
        raise ValueError(f"table has no columns: {schema}.{table}")
    return ", ".join(quote_identifier(column) for column in columns)


async def _column_names(cur, schema: str, table: str) -> list[str]:
    await cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
        "ORDER BY ordinal_position",
        (schema, table),
    )
    return [str(row[0]) for row in await cur.fetchall()]


async def column_signature(cur, schema: str, table: str) -> list[dict[str, Any]]:
    """Return the structural parts that must match before copying a table."""
    await cur.execute(
        "SELECT column_name, column_type, is_nullable, column_default, extra, "
        "character_set_name, collation_name "
        "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
        "ORDER BY ordinal_position",
        (schema, table),
    )
    result: list[dict[str, Any]] = []
    for row in await cur.fetchall():
        result.append({
            "name": str(row[0]),
            "type": str(row[1]),
            "nullable": str(row[2]),
            "default": None if row[3] is None else str(row[3]),
            "extra": str(row[4] or ""),
            "charset": row[5],
            "collation": row[6],
        })
    return result


def structure_matches(source: list[dict[str, Any]], target: list[dict[str, Any]]) -> bool:
    return source == target


async def ensure_target_structure(cur, source_schema: str, target_schema: str, table: str) -> dict[str, Any]:
    """Create a missing target table or reject an incompatible one."""
    source_exists = await table_exists(cur, source_schema, table)
    target_exists = await table_exists(cur, target_schema, table)
    if not source_exists:
        return {
            "source_exists": False,
            "target_exists": target_exists,
            "structure_consistent": not target_exists,
            "source_columns": [],
            "target_columns": await column_signature(cur, target_schema, table) if target_exists else [],
        }
    source_columns = await column_signature(cur, source_schema, table)
    if not target_exists:
        await cur.execute(
            f"CREATE TABLE {quote_identifier(target_schema)}.{quote_identifier(table)} "
            f"LIKE {quote_identifier(source_schema)}.{quote_identifier(table)}"
        )
        target_exists = True
    target_columns = await column_signature(cur, target_schema, table)
    if not structure_matches(source_columns, target_columns):
        raise RuntimeError(f"迁移停止：{source_schema}.{table} 与 {target_schema}.{table} 表结构不一致")
    return {
        "source_exists": True,
        "target_exists": target_exists,
        "structure_consistent": True,
        "source_columns": source_columns,
        "target_columns": target_columns,
    }


async def ensure_state_table(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _domain_migration_state (
            domain VARCHAR(30) PRIMARY KEY,
            source_schema VARCHAR(64) NOT NULL,
            target_schema VARCHAR(64) NOT NULL,
            status VARCHAR(20) NOT NULL,
            switched_at DATETIME DEFAULT NULL,
            checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            detail_json JSON NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)


async def migrate_domain(domain: str, apply: bool, chunk_size: int) -> dict[str, Any]:
    source_schema, target_schema, tables = DOMAIN_TABLES[domain]
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            if apply:
                await cur.execute(f"CREATE DATABASE IF NOT EXISTS {quote_identifier(target_schema)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            result = []
            for table in tables:
                if apply:
                    result.append(await copy_table(cur, source_schema, target_schema, table, chunk_size))
                else:
                    result.append({"table": table, "status": "dry_run", "source": source_schema, "target": target_schema})
            detail = {"tables": result, "apply": apply}
            if apply:
                await ensure_state_table(cur)
                await cur.execute(
                    "INSERT INTO _domain_migration_state (domain, source_schema, target_schema, status, checked_at, detail_json) "
                    "VALUES (%s,%s,%s,%s,UTC_TIMESTAMP(),%s) ON DUPLICATE KEY UPDATE source_schema=VALUES(source_schema), "
                    "target_schema=VALUES(target_schema), status=VALUES(status), checked_at=VALUES(checked_at), detail_json=VALUES(detail_json)",
                    (domain, source_schema, target_schema, "copied", json.dumps(detail, ensure_ascii=False)),
                )
            return {"domain": domain, "source_schema": source_schema, "target_schema": target_schema, **detail}
    finally:
        await close_connection(conn)


async def verify_domain(domain: str) -> dict[str, Any]:
    source_schema, target_schema, tables = DOMAIN_TABLES[domain]
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            checks = []
            for table in tables:
                source = f"{quote_identifier(source_schema)}.{quote_identifier(table)}"
                target = f"{quote_identifier(target_schema)}.{quote_identifier(table)}"
                source_exists = await table_exists(cur, source_schema, table)
                target_exists = await table_exists(cur, target_schema, table)
                source_count = target_count = None
                source_bounds = target_bounds = None
                source_columns = target_columns = []
                structure_consistent = False
                if source_exists:
                    source_columns = await column_signature(cur, source_schema, table)
                    await cur.execute(f"SELECT COUNT(*) FROM {source}")
                    source_count = int((await cur.fetchone())[0])
                    primary = await primary_key_column(cur, source_schema, table)
                    if primary:
                        await cur.execute(f"SELECT MIN({quote_identifier(primary)}), MAX({quote_identifier(primary)}) FROM {source}")
                        source_bounds = await cur.fetchone()
                if target_exists:
                    target_columns = await column_signature(cur, target_schema, table)
                    await cur.execute(f"SELECT COUNT(*) FROM {target}")
                    target_count = int((await cur.fetchone())[0])
                    primary = await primary_key_column(cur, target_schema, table)
                    if primary:
                        await cur.execute(f"SELECT MIN({quote_identifier(primary)}), MAX({quote_identifier(primary)}) FROM {target}")
                        target_bounds = await cur.fetchone()
                if not source_exists and not target_exists:
                    structure_consistent = True
                elif source_exists and target_exists:
                    structure_consistent = structure_matches(source_columns, target_columns)
                checks.append({
                    "table": table,
                    "source_exists": source_exists,
                    "target_exists": target_exists,
                    "source_count": source_count,
                    "target_count": target_count,
                    "source_bounds": source_bounds,
                    "target_bounds": target_bounds,
                    "source_columns": source_columns,
                    "target_columns": target_columns,
                    "structure_consistent": structure_consistent,
                    "consistent": structure_consistent and (
                        not source_exists and not target_exists
                        or source_count == target_count and source_bounds == target_bounds
                    ),
                })
            return {"domain": domain, "source_schema": source_schema, "target_schema": target_schema, "tables": checks,
                    "consistent": all(item["consistent"] for item in checks)}
    finally:
        await close_connection(conn)


async def main_async(args: argparse.Namespace) -> None:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            if args.command == "measure":
                print(json.dumps(await measure_schema(cur, args.schema), ensure_ascii=False, indent=2, default=str))
                return
    finally:
        await close_connection(conn)
    if args.command == "migrate":
        if not args.apply:
            print(json.dumps(await migrate_domain(args.domain, False, args.chunk_size), ensure_ascii=False, indent=2))
            return
        print(json.dumps(await migrate_domain(args.domain, True, args.chunk_size), ensure_ascii=False, indent=2))
    if args.command == "verify":
        print(json.dumps(await verify_domain(args.domain), ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("measure")
    measure.add_argument("--schema", required=True)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--domain", choices=sorted(DOMAIN_TABLES), required=True)
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--chunk-size", type=int, default=1000)
    verify = sub.add_parser("verify")
    verify.add_argument("--domain", choices=sorted(DOMAIN_TABLES), required=True)
    return parser


if __name__ == "__main__":
    arguments = build_parser().parse_args()
    asyncio.run(main_async(arguments))
