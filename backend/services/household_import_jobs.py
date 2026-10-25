"""Persistent background jobs for household workbook confirmation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from database import db_manager


_tasks: set[asyncio.Task] = set()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def public_household_import_job(row: tuple | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "batch_id": int(row[0]),
        "status": str(row[1]),
        "confirm_status": str(row[2] or ""),
        "confirm_phase": str(row[3] or ""),
        "processed_count": int(row[4] or 0),
        "total_count": int(row[5] or 0),
        "error_code": str(row[6] or "") or None,
        "error_message": str(row[7] or "") or None,
        "result": _json(row[8]),
        "started_at": _iso(row[9]),
        "finished_at": _iso(row[10]),
        "updated_at": _iso(row[11]),
    }


_SELECT = (
    "SELECT id,status,confirm_status,confirm_phase,confirm_processed_count,confirm_total_count,"
    "confirm_error_code,confirm_error_message,confirm_result_json,confirm_started_at,"
    "confirm_finished_at,confirm_updated_at FROM registry_source_batches "
    "WHERE id=%s AND source_type='household'"
)


async def get_household_import_job(batch_id: int) -> dict[str, Any] | None:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT, (batch_id,))
            return public_household_import_job(await cur.fetchone())


async def _update_job(batch_id: int, **values: Any) -> None:
    if not values:
        return
    pool = db_manager.get_pool("registry")
    assignments = ",".join(f"{key}=%s" for key in values)
    params = tuple(values.values()) + (batch_id,)
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE registry_source_batches SET {assignments},confirm_updated_at=UTC_TIMESTAMP() "
                "WHERE id=%s AND source_type='household'",
                params,
            )
        await conn.commit()


async def _run_household_import_job(batch_id: int, user_id: int) -> None:
    await _update_job(
        batch_id,
        confirm_status="running",
        confirm_phase="processing",
        confirm_error_code="",
        confirm_error_message="",
        confirm_started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        confirm_finished_at=None,
    )
    try:
        from routers.registry_extended import confirm_household_import

        pool = db_manager.get_pool("registry")
        async with pool.acquire() as conn:
            result = await confirm_household_import(
                batch_id,
                None,
                {"id": int(user_id)},
                conn,
            )
        await _update_job(
            batch_id,
            confirm_status="completed",
            confirm_phase="finished",
            confirm_processed_count=int(result.get("total_imported_count") or result.get("imported_count") or 0),
            confirm_result_json=json.dumps(result, ensure_ascii=False),
            confirm_finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        await _update_job(
            batch_id,
            confirm_status="failed",
            confirm_phase="finished",
            confirm_error_code="confirm_failed",
            confirm_error_message="户号表确认任务失败，请重新确认或联系管理员",
            confirm_finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )


def launch_household_import_job(batch_id: int, user_id: int) -> None:
    task = asyncio.create_task(_run_household_import_job(batch_id, user_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def stop_household_import_tasks() -> None:
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def recover_interrupted_household_imports() -> int:
    try:
        pool = db_manager.get_pool("registry")
    except ValueError:
        return 0
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE registry_source_batches SET confirm_status='failed',confirm_phase='finished',"
                "confirm_error_code='service_restarted',confirm_error_message='服务重启，确认任务未完成，请重新确认',"
                "confirm_finished_at=UTC_TIMESTAMP(),confirm_updated_at=UTC_TIMESTAMP() "
                "WHERE source_type='household' AND confirm_status IN ('queued','running')"
            )
            changed = int(cur.rowcount or 0)
        await conn.commit()
    return changed
