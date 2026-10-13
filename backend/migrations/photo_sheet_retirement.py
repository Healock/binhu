"""照片调取业务退役维护工具。

The command is deliberately conservative.  It only removes the photo-specific
tables, photo work orders and their private attachments.  It never touches
other workflow types or the historical Tencent migration material.

Run from ``backend`` during an approved maintenance window::

    python -m migrations.photo_sheet_retirement measure
    python -m migrations.photo_sheet_retirement migrate --apply --expect-photo-tickets 134 --confirm-empty-photo-tables
    python -m migrations.photo_sheet_retirement verify

The local development machine has no project MySQL; this module is therefore
contract-tested only and must be run through the production maintenance gate.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shutil
import uuid
from pathlib import Path

import aiomysql

from config import settings
from services.workflow_support import resolve_attachment
from services.photo_import import resolve_photo_import_zip


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


async def measure_with_cursor(cur, *, expected_photo_tickets: int | None = None) -> dict:
    ticket_ids = await photo_ticket_ids(cur)
    table_counts = {table: await count_table(cur, table) for table in PHOTO_TABLES}
    attachments = await attachment_inventory(cur, ticket_ids)
    zip_keys = await zip_inventory(cur)
    await cur.execute("SELECT COUNT(*) FROM work_orders WHERE type_code<>'photo_request'")
    non_photo = int((await cur.fetchone())[0] or 0)
    return {
        "expected_photo_tickets": expected_photo_tickets,
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


def _retirement_stage_root() -> Path:
    return Path(settings.WORKFLOW_ATTACHMENT_DIR).resolve() / ".photo-sheet-retirement"


def _stage_and_remove(path: Path, root: Path, stage_root: Path) -> tuple[Path, Path]:
    relative = path.relative_to(root)
    staged = stage_root / relative
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, staged)
    path.unlink()
    return path, staged


def _restore_staged(entries: list[tuple[Path, Path]]) -> None:
    for original, staged in reversed(entries):
        if not staged.is_file():
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, original)


def _discard_stage(stage_root: Path) -> None:
    if stage_root.exists():
        shutil.rmtree(stage_root, ignore_errors=False)


async def run_apply(
    *,
    expected_photo_tickets: int,
    confirm_empty_photo_tables: bool,
) -> dict:
    if not confirm_empty_photo_tables:
        raise RuntimeError(
            "migrate --apply 必须显式提供 --confirm-empty-photo-tables，"
            "确认九张照片专属表允许整表清空"
        )
    conn = await open_connection()
    staged_files: list[tuple[Path, Path]] = []
    missing_attachment_files = 0
    missing_zip_files = 0
    stage_root = _retirement_stage_root() / uuid.uuid4().hex
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            before = await measure_with_cursor(
                cur,
                expected_photo_tickets=expected_photo_tickets,
            )
            if before["photo_work_orders"] != expected_photo_tickets:
                raise RuntimeError(
                    "照片工单数量不符合预期，已停止："
                    f"expected={expected_photo_tickets}, actual={before['photo_work_orders']}"
                )
            if before["shared_attachment_rows"]:
                raise RuntimeError("发现被其他业务工单共享的照片附件，已停止")
            ticket_ids = await photo_ticket_ids(cur, lock=True)
            inventory = await attachment_inventory(cur, ticket_ids)
            attachment_keys = list(inventory["attachment_keys"])
            zip_keys = await zip_inventory(cur)

            attachment_root = Path(settings.WORKFLOW_ATTACHMENT_DIR).resolve()
            zip_root = Path(settings.WORKFLOW_PHOTO_IMPORT_DIR).resolve()
            for key in attachment_keys:
                try:
                    path = resolve_attachment(key)
                except FileNotFoundError:
                    missing_attachment_files += 1
                    continue
                staged_files.append(_stage_and_remove(path, attachment_root, stage_root / "attachments"))
            for key in zip_keys:
                try:
                    path = resolve_photo_import_zip(key)
                except FileNotFoundError:
                    missing_zip_files += 1
                    continue
                staged_files.append(_stage_and_remove(path, zip_root, stage_root / "zips"))
            deleted = await delete_photo_rows(cur, ticket_ids)
        await conn.commit()
    except Exception:
        _restore_staged(staged_files)
        await conn.rollback()
        if stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)
        raise
    finally:
        await close_connection(conn)
    _discard_stage(stage_root)
    return {
        "command": "migrate",
        "expected_photo_tickets": expected_photo_tickets,
        "deleted": deleted,
        "attachment_keys": len(attachment_keys),
        "missing_attachment_files": missing_attachment_files,
        "zip_keys": len(zip_keys),
        "missing_zip_files": missing_zip_files,
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
    migrate.add_argument(
        "--expect-photo-tickets",
        type=int,
        default=None,
        help="执行清理前要求数据库中的照片工单数量精确匹配该值，例如 134",
    )
    migrate.add_argument(
        "--confirm-empty-photo-tables",
        action="store_true",
        help="确认九张照片专属表为退役专属表，允许整表清空",
    )
    sub.add_parser("verify", help="只读核验照片调取数据已清理")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "measure":
        result = await run_measure()
    elif args.command == "verify":
        result = await run_verify()
    elif args.apply:
        if args.expect_photo_tickets is None:
            raise RuntimeError("migrate --apply 必须显式提供 --expect-photo-tickets")
        result = await run_apply(
            expected_photo_tickets=args.expect_photo_tickets,
            confirm_empty_photo_tables=args.confirm_empty_photo_tables,
        )
    else:
        result = await run_measure()
        result["command"] = "migrate-preview"
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
