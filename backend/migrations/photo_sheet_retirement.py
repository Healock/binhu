"""照片调取业务退役维护工具。

The command is deliberately conservative.  It only removes the photo-specific
tables, photo work orders and their private attachments.  It never touches
other workflow types or the historical Tencent migration material.

Run from ``backend`` during an approved maintenance window::

    python -m migrations.photo_sheet_retirement measure
    python -m migrations.photo_sheet_retirement migrate --apply --expect-photo-tickets <measure-count> --confirm-empty-photo-tables
    python -m migrations.photo_sheet_retirement verify

The local development machine has no project MySQL; this module is therefore
contract-tested only and must be run through the production maintenance gate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
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

_SAFE_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _retirement_root() -> Path:
    return Path(settings.WORKFLOW_ATTACHMENT_DIR).resolve() / ".photo-sheet-retirement"


def _retirement_state_root() -> Path:
    # Persist status on the same durable private volume as the staging directory.
    return Path(settings.WORKFLOW_ATTACHMENT_DIR).resolve() / ".photo-sheet-retirement-state"


def _state_path(run_id: str) -> Path:
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError("invalid_photo_retirement_run_id")
    return _retirement_state_root() / f"{run_id}.json"


def _write_state(run_id: str, payload: dict) -> None:
    root = _retirement_state_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    target = _state_path(run_id)
    fd, temp_name = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".partial", dir=root)
    temp_path = Path(temp_name)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        temp_path.unlink(missing_ok=True)


def _load_retirement_state() -> dict:
    state_root = _retirement_state_root()
    states: list[dict] = []
    invalid_state_count = 0
    if state_root.exists():
        for path in state_root.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if (
                    item.get("kind") != "photo_sheet_retirement"
                    or item.get("status") not in {
                        "prepared", "database_committed_cleanup_pending",
                        "rollback_cleanup_pending", "rolled_back", "completed",
                    }
                ):
                    invalid_state_count += 1
                    continue
                states.append(item)
            except (OSError, ValueError, TypeError):
                invalid_state_count += 1
    pending = sum(
        item.get("status") in {
            "prepared", "database_committed_cleanup_pending", "rollback_cleanup_pending"
        }
        for item in states
    )
    orphan_stages = 0
    stage_root = _retirement_root()
    if stage_root.exists():
        known_runs = {str(item.get("run_id") or "") for item in states}
        orphan_stages = sum(
            1 for path in stage_root.iterdir() if path.name not in known_runs
        )
    residue_files = 0
    for item in states:
        run_id = str(item.get("run_id") or "")
        if run_id and (stage_root / run_id).exists() and item.get("status") == "completed":
            residue_files += 1
        if item.get("status") != "completed":
            continue
        for entry in item.get("files") or []:
            try:
                original = Path(entry["original"])
                staged = Path(entry["staged"])
            except (KeyError, TypeError):
                residue_files += 1
                continue
            if original.exists() or staged.exists():
                residue_files += 1
    return {
        "pending_runs": int(pending),
        "orphan_stage_directories": int(orphan_stages),
        "file_residue_count": int(residue_files),
        "invalid_state_files": int(invalid_state_count),
    }
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
        return {
            "photo_attachment_rows": 0,
            "attachment_keys": [],
            "attachment_hashes": {},
            "invalid_storage_key_rows": 0,
            "shared_attachment_rows": 0,
        }
    placeholders = ",".join(["%s"] * len(ticket_ids))
    await cur.execute(
        "SELECT storage_key,sha256,COUNT(*),SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) "
        "FROM work_order_attachments "
        f"WHERE work_order_id IN ({placeholders}) "
        "GROUP BY storage_key,sha256",
        tuple(ticket_ids),
    )
    rows = await cur.fetchall()
    hashes: dict[str, set[str]] = {}
    invalid_storage_key_rows = 0
    photo_attachment_rows = 0
    for row in rows:
        photo_attachment_rows += int(row[2] or 0)
        active_rows = int(row[3] or 0)
        if not active_rows:
            continue
        key = str(row[0] or "").strip()
        if not key:
            invalid_storage_key_rows += active_rows
            continue
        hashes.setdefault(key, set()).add(str(row[1] or "").strip().lower())
    keys = sorted(hashes)
    shared = 0
    for key in keys:
        await cur.execute(
            "SELECT COUNT(*) FROM work_order_attachments attachment "
            "JOIN work_orders order_row ON order_row.id=attachment.work_order_id "
            "WHERE attachment.storage_key=%s AND attachment.deleted_at IS NULL "
            "AND order_row.type_code<>'photo_request'",
            (key,),
        )
        row = await cur.fetchone()
        shared += int(row[0] or 0) if row else 0
    return {
        "photo_attachment_rows": photo_attachment_rows,
        "attachment_keys": keys,
        "attachment_hashes": hashes,
        "invalid_storage_key_rows": invalid_storage_key_rows,
        "shared_attachment_rows": shared,
    }


async def zip_inventory(cur) -> dict[str, object]:
    if not await table_exists(cur, "photo_request_import_batches"):
        return {"hashes": {}, "invalid_storage_key_rows": 0}
    await cur.execute("SELECT storage_key,zip_sha256 FROM photo_request_import_batches")
    result: dict[str, set[str]] = {}
    invalid_storage_key_rows = 0
    for row in await cur.fetchall():
        key = str(row[0] or "").strip()
        if not key:
            invalid_storage_key_rows += 1
            continue
        result.setdefault(key, set()).add(str(row[1] or "").strip().lower())
    return {"hashes": result, "invalid_storage_key_rows": invalid_storage_key_rows}


def _file_reference_reconciliation(
    references: dict[str, set[str]],
    resolver,
) -> dict[str, int]:
    missing = 0
    hash_mismatches = 0
    invalid_hashes = 0
    conflicting_hashes = 0
    checked = 0
    for storage_key, expected_values in references.items():
        expected = {value for value in expected_values if value}
        if len(expected_values) != 1 or len(expected) != 1:
            if len(expected_values) > 1:
                conflicting_hashes += 1
            else:
                invalid_hashes += 1
            continue
        expected_sha = next(iter(expected))
        if not _SHA256.fullmatch(expected_sha):
            invalid_hashes += 1
            continue
        try:
            path = resolver(storage_key)
        except (FileNotFoundError, OSError, ValueError):
            missing += 1
            continue
        checked += 1
        try:
            actual_sha = _sha256_file(path)
        except OSError:
            missing += 1
            continue
        if actual_sha != expected_sha:
            hash_mismatches += 1
    return {
        "referenced": len(references),
        "checked": checked,
        "missing": missing,
        "sha256_mismatches": hash_mismatches,
        "invalid_sha256": invalid_hashes,
        "conflicting_sha256": conflicting_hashes,
    }


async def measure_with_cursor(cur, *, expected_photo_tickets: int | None = None) -> dict:
    ticket_ids = await photo_ticket_ids(cur)
    table_counts = {table: await count_table(cur, table) for table in PHOTO_TABLES}
    attachments = await attachment_inventory(cur, ticket_ids)
    zip_data = await zip_inventory(cur)
    zip_hashes = zip_data["hashes"]
    file_reconciliation = {
        "attachments": _file_reference_reconciliation(
            attachments["attachment_hashes"], resolve_attachment,
        ),
        "photo_import_zips": _file_reference_reconciliation(
            zip_hashes, resolve_photo_import_zip,
        ),
        "cleanup_state": _load_retirement_state(),
    }
    file_reconciliation["attachments"]["invalid_storage_key_rows"] = attachments[
        "invalid_storage_key_rows"
    ]
    file_reconciliation["photo_import_zips"]["invalid_storage_key_rows"] = zip_data[
        "invalid_storage_key_rows"
    ]
    await cur.execute("SELECT COUNT(*) FROM work_orders WHERE type_code<>'photo_request'")
    non_photo = int((await cur.fetchone())[0] or 0)
    return {
        "expected_photo_tickets": expected_photo_tickets,
        "photo_work_orders": len(ticket_ids),
        "non_photo_work_orders": non_photo,
        "table_counts": table_counts,
        "photo_attachment_rows": attachments["photo_attachment_rows"],
        "attachment_key_count": len(attachments["attachment_keys"]),
        "invalid_attachment_storage_key_rows": attachments["invalid_storage_key_rows"],
        "shared_attachment_rows": attachments["shared_attachment_rows"],
        "photo_zip_file_count": len(zip_hashes),
        "invalid_photo_zip_storage_key_rows": zip_data["invalid_storage_key_rows"],
        "file_reconciliation": file_reconciliation,
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
    counts: dict[str, int] = {}
    if not ticket_ids:
        for table in PHOTO_TABLES:
            if not await table_exists(cur, table):
                continue
            await cur.execute(f"DELETE FROM `{table}`")
            counts[table] = int(cur.rowcount or 0)
        return counts
    placeholders = ",".join(["%s"] * len(ticket_ids))
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
    return _retirement_root()


def _stage_and_remove(
    path: Path,
    root: Path,
    stage_root: Path,
    expected_sha256: str,
) -> dict[str, str]:
    if not _SHA256.fullmatch(expected_sha256):
        raise RuntimeError("照片附件缺少有效 SHA-256，已停止")
    relative = path.relative_to(root)
    staged = stage_root / relative
    staged.parent.mkdir(parents=True, exist_ok=True)
    if _sha256_file(path) != expected_sha256:
        raise RuntimeError("照片文件 SHA-256 与数据库记录不匹配，已停止")
    shutil.copy2(path, staged)
    if _sha256_file(staged) != expected_sha256:
        staged.unlink(missing_ok=True)
        raise RuntimeError("照片暂存文件 SHA-256 校验失败，已停止")
    path.unlink()
    return {
        "original": str(path),
        "staged": str(staged),
        "sha256": expected_sha256,
    }


def _restore_staged(entries: list[dict[str, str]]) -> None:
    for item in reversed(entries):
        original = Path(item["original"])
        staged = Path(item["staged"])
        if not staged.is_file():
            continue
        if staged.is_symlink() or _sha256_file(staged) != item["sha256"]:
            raise RuntimeError("照片暂存文件 SHA-256 不匹配，拒绝恢复")
        if original.exists():
            if original.is_symlink() or _sha256_file(original) != item["sha256"]:
                raise RuntimeError("照片原文件路径已被其他内容占用，拒绝覆盖")
            staged.unlink()
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, original)


def _discard_stage(stage_root: Path) -> None:
    if stage_root.exists():
        shutil.rmtree(stage_root, ignore_errors=False)


def _discard_stage_with_state(run_id: str, stage_root: Path, state: dict) -> tuple[bool, str]:
    state.update({
        "status": "database_committed_cleanup_pending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _write_state(run_id, state)
    try:
        _discard_stage(stage_root)
    except Exception as exc:
        state.update({
            "status": "database_committed_cleanup_pending",
            "error_code": type(exc).__name__,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_state(run_id, state)
        return False, type(exc).__name__
    state.update({
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    _write_state(run_id, state)
    return True, ""


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
    run_id = uuid.uuid4().hex
    staged_files: list[dict[str, str]] = []
    stage_root = _retirement_stage_root() / run_id
    state = {
        "kind": "photo_sheet_retirement",
        "run_id": run_id,
        "status": "prepared",
        "files": staged_files,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    state_written = False
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
            reconciliation = before["file_reconciliation"]
            bad_files = any(
                int(reconciliation[kind][field])
                for kind in ("attachments", "photo_import_zips")
                for field in (
                    "missing", "sha256_mismatches", "invalid_sha256",
                    "conflicting_sha256", "invalid_storage_key_rows",
                )
            )
            cleanup_state = reconciliation["cleanup_state"]
            if bad_files or before["invalid_attachment_storage_key_rows"]:
                raise RuntimeError("照片文件存在缺失、SHA-256 不匹配或无效引用，已停止")
            if any(cleanup_state.values()):
                raise RuntimeError("检测到未完成的照片退役清理状态，已停止")
            ticket_ids = await photo_ticket_ids(cur, lock=True)
            inventory = await attachment_inventory(cur, ticket_ids)
            attachment_keys = list(inventory["attachment_keys"])
            attachment_hashes = inventory["attachment_hashes"]
            zip_data = await zip_inventory(cur)
            if zip_data["invalid_storage_key_rows"]:
                raise RuntimeError("照片导入压缩包存在无效存储路径，已停止")
            zip_hashes = zip_data["hashes"]
            zip_keys = sorted(zip_hashes)

            attachment_root = Path(settings.WORKFLOW_ATTACHMENT_DIR).resolve()
            zip_root = Path(settings.WORKFLOW_PHOTO_IMPORT_DIR).resolve()
            for key in attachment_keys:
                hashes = attachment_hashes[key]
                if len(hashes) != 1:
                    raise RuntimeError("照片附件存在冲突的 SHA-256 记录，已停止")
                path = resolve_attachment(key)
                staged = _stage_and_remove(
                    path, attachment_root, stage_root / "attachments", next(iter(hashes)),
                )
                staged_files.append(staged)
            for key in zip_keys:
                hashes = zip_hashes[key]
                if len(hashes) != 1:
                    raise RuntimeError("照片导入压缩包存在冲突的 SHA-256 记录，已停止")
                path = resolve_photo_import_zip(key)
                staged = _stage_and_remove(
                    path, zip_root, stage_root / "zips", next(iter(hashes)),
                )
                staged_files.append(staged)
            state["files"] = staged_files
            _write_state(run_id, state)
            state_written = True
            deleted = await delete_photo_rows(cur, ticket_ids)
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        restore_error = None
        try:
            _restore_staged(staged_files)
            _discard_stage(stage_root)
        except Exception as cleanup_exc:
            restore_error = cleanup_exc
        if state_written:
            state.update({
                "status": "rollback_cleanup_pending" if restore_error else "rolled_back",
                "error_code": type(restore_error or exc).__name__,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            _write_state(run_id, state)
        if restore_error:
            raise RuntimeError("照片退役事务已回滚，但暂存文件恢复/清理未完成；verify 将报告待处理状态") from restore_error
        raise
    finally:
        await close_connection(conn)
    state["deleted"] = deleted
    cleanup_ok, cleanup_error = _discard_stage_with_state(run_id, stage_root, state)
    if not cleanup_ok:
        return {
            "command": "migrate",
            "run_id": run_id,
            "status": "database_committed_cleanup_pending",
            "ok": False,
            "cleanup_error": cleanup_error,
            "deleted": deleted,
            "staged_file_count": len(staged_files),
        }
    return {
        "command": "migrate",
        "run_id": run_id,
        "status": "completed",
        "ok": True,
        "expected_photo_tickets": expected_photo_tickets,
        "deleted": deleted,
        "attachment_keys": len(attachment_keys),
        "zip_keys": len(zip_keys),
        "file_reconciliation": before["file_reconciliation"],
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
                "photo_files_reconciled": all(
                    int(result["file_reconciliation"][kind][field]) == 0
                    for kind in ("attachments", "photo_import_zips")
                    for field in (
                        "missing", "sha256_mismatches", "invalid_sha256",
                        "conflicting_sha256", "invalid_storage_key_rows",
                    )
                ),
                "cleanup_state_clear": not any(result["file_reconciliation"]["cleanup_state"].values()),
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
    if result.get("ok") is False:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
