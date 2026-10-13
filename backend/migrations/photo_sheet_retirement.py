"""照片调取业务退役维护工具。

The command is deliberately conservative.  It only removes the photo-specific
tables, photo work orders and their private attachments.  It never touches
other workflow types or the historical Tencent migration material.

Run from ``backend`` during an approved maintenance window::

    python -m migrations.photo_sheet_retirement measure
    python -m migrations.photo_sheet_retirement migrate --apply
    python -m migrations.photo_sheet_retirement verify

The local development machine has no project MySQL; this module is therefore
contract-tested only and must be run through the production maintenance gate.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from pathlib import Path

import aiomysql

from config import settings
from services.workflow_support import remove_attachment, resolve_attachment
from services.photo_import import remove_photo_import_zip, resolve_photo_import_zip


PHOTO_TABLES = (
    "photo_request_details",
    "photo_request_import_batches",
    "photo_request_import_items",
    "photo_sheet_sources",
    "photo_sheet_batches",
    "photo_sheet_rows",
    "photo_sheet_outbox",
    "photo_sheet_sync_runs",
    "photo_sheet_conflicts",
)
EXPECTED_PHOTO_TICKETS = 134


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
        db=settings.MYSQL_WORKFLOW_DB,
        autocommit=False,
        charset="utf8mb4",
    )


async def table_exists(cur, table: str) -> bool:
    await cur.execute("SHOW TABLES LIKE %s", (table,))
    return bool(await cur.fetchone())


async def count_table(cur, table: str) -> int:
    if not await table_exists(cur, table):
        return 0
    await cur.execute(f"SELECT COUNT(*) FROM `{table}`")
    row = await cur.fetchone()
    return int(row[0] or 0) if row else 0


async def photo_ticket_ids(cur, *, lock: bool = False) -> list[int]:
    await cur.execute(
        "SELECT id FROM work_orders WHERE type_code='photo_request' ORDER BY id"
        + (" FOR UPDATE" if lock else ""),
    )
    return [int(row[0]) for row in await cur.fetchall()]


async def attachment_inventory(cur, ticket_ids: list[int]) -> dict[str, object]:
    if not ticket_ids or not await table_exists(cur, "work_order_attachments"):
        return {"photo_attachment_rows": 0, "attachment_keys": [], "shared_attachment_rows": 0}
    placeholders = ",".join(["%s"] * len(ticket_ids))
    await cur.execute(
        "SELECT storage_key,COUNT(*) FROM work_order_attachments "
        f"WHERE work_order_id IN ({placeholders}) AND deleted_at IS NULL "
        "GROUP BY storage_key",
        tuple(ticket_ids),
    )
    rows = await cur.fetchall()
    keys = [str(row[0]) for row in rows if row[0]]
    shared = 0
    for key in keys:
        await cur.execute(
            "SELECT COUNT(*) FROM work_order_attachments attachment "
            "JOIN work_orders order_row ON order_row.id=attachment.work_order_id "
            "WHERE attachment.storage_key=%s AND order_row.type_code<>'photo_request' "
            "AND attachment.deleted_at IS NULL",
            (key,),
        )
        row = await cur.fetchone()
        shared += int(row[0] or 0) if row else 0
    return {
        "photo_attachment_rows": sum(int(row[1] or 0) for row in rows),
        "attachment_keys": keys,
        "shared_attachment_rows": shared,
    }


async def zip_inventory(cur) -> list[str]:
    if not await table_exists(cur, "photo_request_import_batches"):
        return []
    await cur.execute("SELECT storage_key FROM photo_request_import_batches WHERE storage_key<>''")
    return [str(row[0]) for row in await cur.fetchall() if row[0]]


async def measure_with_cursor(cur) -> dict:
    ticket_ids = await photo_ticket_ids(cur)
    table_counts = {table: await count_table(cur, table) for table in PHOTO_TABLES}
    attachments = await attachment_inventory(cur, ticket_ids)
    zip_keys = await zip_inventory(cur)
    await cur.execute("SELECT COUNT(*) FROM work_orders WHERE type_code<>'photo_request'")
    non_photo = int((await cur.fetchone())[0] or 0)
    return {
        "expected_photo_tickets": EXPECTED_PHOTO_TICKETS,
        "photo_work_orders": len(ticket_ids),
        "non_photo_work_orders": non_photo,
        "table_counts": table_counts,
        "photo_attachment_rows": attachments["photo_attachment_rows"],
        "attachment_key_count": len(attachments["attachment_keys"]),
        "shared_attachment_rows": attachments["shared_attachment_rows"],
        "photo_zip_file_count": len(zip_keys),
        "backup_scope": "WorkflowData + private photo attachment/ZIP directories",
        "external_tencent_materials": "untouched",
    }


async def run_measure() -> dict:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            result = await measure_with_cursor(cur)
        await conn.rollback()
        return {"command": "measure", **result}
    finally:
        await close_connection(conn)


async def delete_photo_rows(cur, ticket_ids: list[int]) -> dict[str, int]:
    placeholders = ",".join(["%s"] * len(ticket_ids))
    counts: dict[str, int] = {}
    child_specs = (
        ("work_order_reminders", "work_order_id"),
        ("work_order_claims", "work_order_id"),
        ("work_order_comments", "work_order_id"),
        ("work_order_events", "work_order_id"),
        ("work_order_links", "work_order_id"),
        ("work_order_steps", "work_order_id"),
        ("work_order_attachments", "work_order_id"),
    )
    for table, column in child_specs:
        if not await table_exists(cur, table):
            continue
        await cur.execute(
            f"DELETE FROM `{table}` WHERE `{column}` IN ({placeholders})",
            tuple(ticket_ids),
        )
        counts[table] = int(cur.rowcount or 0)
    if await table_exists(cur, "work_orders"):
        await cur.execute(
            f"DELETE FROM work_orders WHERE id IN ({placeholders}) AND type_code='photo_request'",
            tuple(ticket_ids),
        )
        counts["work_orders"] = int(cur.rowcount or 0)
    for table in PHOTO_TABLES:
        if not await table_exists(cur, table):
            continue
        await cur.execute(f"DELETE FROM `{table}`")
        counts[table] = int(cur.rowcount or 0)
    return counts


async def run_apply() -> dict:
    conn = await open_connection()
    attachment_keys: list[str] = []
    zip_keys: list[str] = []
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            before = await measure_with_cursor(cur)
            if before["photo_work_orders"] != EXPECTED_PHOTO_TICKETS:
                raise RuntimeError(
                    "照片工单数量不符合预期，已停止："
                    f"expected={EXPECTED_PHOTO_TICKETS}, actual={before['photo_work_orders']}"
                )
            if before["shared_attachment_rows"]:
                raise RuntimeError("发现被其他业务工单共享的照片附件，已停止")
            ticket_ids = await photo_ticket_ids(cur, lock=True)
            inventory = await attachment_inventory(cur, ticket_ids)
            attachment_keys = list(inventory["attachment_keys"])
            zip_keys = await zip_inventory(cur)
            deleted = await delete_photo_rows(cur, ticket_ids)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await close_connection(conn)

    missing_files = 0
    failed_files = 0
    for key in attachment_keys:
        try:
            resolve_attachment(key)
        except FileNotFoundError:
            missing_files += 1
        else:
            if not remove_attachment(key):
                failed_files += 1
    missing_zips = 0
    failed_zips = 0
    for key in zip_keys:
        try:
            resolve_photo_import_zip(key)
        except FileNotFoundError:
            missing_zips += 1
        else:
            try:
                remove_photo_import_zip(key)
            except OSError:
                failed_zips += 1
    if failed_files or failed_zips:
        raise RuntimeError(
            f"照片文件删除失败：attachments={failed_files}, zips={failed_zips}；请保留数据库备份并人工处理"
        )
    return {
        "command": "migrate",
        "expected_photo_tickets": EXPECTED_PHOTO_TICKETS,
        "deleted": deleted,
        "attachment_keys": len(attachment_keys),
        "missing_attachment_files": missing_files,
        "zip_keys": len(zip_keys),
        "missing_zip_files": missing_zips,
        "backup_scope": "WorkflowData + private photo attachment/ZIP directories",
        "external_tencent_materials": "untouched",
    }


async def run_verify() -> dict:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            result = await measure_with_cursor(cur)
            checks = {
                "photo_tables_empty": all(value == 0 for value in result["table_counts"].values()),
                "photo_work_orders_empty": result["photo_work_orders"] == 0,
                "photo_attachments_empty": result["photo_attachment_rows"] == 0,
                "shared_attachments_empty": result["shared_attachment_rows"] == 0,
                "photo_zips_empty": result["photo_zip_file_count"] == 0,
            }
        await conn.rollback()
    finally:
        await close_connection(conn)
    return {"command": "verify", **result, "checks": checks, "ok": all(checks.values())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", help="只读统计照片调取专属数据")
    migrate = sub.add_parser("migrate", help="预览或执行照片调取退役清理")
    migrate.add_argument("--apply", action="store_true", help="明确授权删除照片调取历史数据")
    sub.add_parser("verify", help="只读核验照片调取数据已清理")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "measure":
        result = await run_measure()
    elif args.command == "verify":
        result = await run_verify()
    elif args.apply:
        result = await run_apply()
    else:
        result = await run_measure()
        result["command"] = "migrate-preview"
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
