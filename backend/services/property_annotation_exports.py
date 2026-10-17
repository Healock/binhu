"""Persistent status and file storage for property annotation exports."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Awaitable, Callable
from uuid import UUID, uuid4

from config import settings
from database import db_manager

_tasks: set[asyncio.Task] = set()
_EXPORT_LOCK = "binhu_registry_property_annotation_export"


def _root() -> Path:
    return Path(settings.REGISTRY_ANNOTATION_EXPORT_DIR).resolve()


def _path(job_id: str) -> Path:
    return _root() / f"{UUID(job_id)}.xlsx"


def _public(row: tuple | None) -> dict | None:
    if not row:
        return None
    return {
        "id": str(row[0]), "status": str(row[1]), "phase": str(row[2]),
        "row_count": int(row[3] or 0), "file_size": int(row[4] or 0),
        "error_code": str(row[5] or ""),
        "created_at": row[6].isoformat() + "Z" if row[6] else None,
        "finished_at": row[7].isoformat() + "Z" if row[7] else None,
        "expires_at": row[8].isoformat() + "Z" if row[8] else None,
    }


async def _delete_expired(cur) -> None:
    await cur.execute(
        "SELECT id FROM registry_property_annotation_exports "
        "WHERE expires_at<=UTC_TIMESTAMP()"
    )
    expired = [str(row[0]) for row in await cur.fetchall()]
    if expired:
        await cur.execute(
            "DELETE FROM registry_property_annotation_exports WHERE expires_at<=UTC_TIMESTAMP()"
        )
    for job_id in expired:
        _path(job_id).unlink(missing_ok=True)


async def create_export_job(requested_by: int, runner: Callable[[str], Awaitable[tuple[int, bytes]]]) -> dict:
    pool = db_manager.get_pool("registry")
    job_id = str(uuid4())
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT GET_LOCK(%s, 5)", (_EXPORT_LOCK,))
            lock = await cur.fetchone()
            if not lock or lock[0] != 1:
                raise RuntimeError("小区标注导出任务正在创建，请稍后重试")
            try:
                await _delete_expired(cur)
                await cur.execute(
                    "SELECT id FROM registry_property_annotation_exports "
                    "WHERE status IN ('queued','running') LIMIT 1"
                )
                if await cur.fetchone():
                    raise RuntimeError("已有小区标注导出任务正在处理，请稍后再试")
                await cur.execute(
                    "INSERT INTO registry_property_annotation_exports "
                    "(id,requested_by,status,phase,expires_at) "
                    "VALUES (%s,%s,'queued','queued',DATE_ADD(UTC_TIMESTAMP(),INTERVAL 24 HOUR))",
                    (job_id, int(requested_by)),
                )
            finally:
                await cur.execute("SELECT RELEASE_LOCK(%s)", (_EXPORT_LOCK,))
                await cur.fetchone()
    task = asyncio.create_task(_run(job_id, runner))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return await get_export_job(job_id)


async def _run(job_id: str, runner: Callable[[str], Awaitable[tuple[int, bytes]]]) -> None:
    pool = db_manager.get_pool("registry")
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE registry_property_annotation_exports SET status='running',phase='querying',"
                    "started_at=UTC_TIMESTAMP() WHERE id=%s AND status='queued'", (job_id,),
                )
        row_count, content = await runner(job_id)
        root = _root()
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        temporary = root / f".{job_id}.tmp"
        await asyncio.to_thread(temporary.write_bytes, content)
        await asyncio.to_thread(os.replace, temporary, _path(job_id))
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE registry_property_annotation_exports SET status='ready',phase='finished',"
                    "row_count=%s,file_size=%s,finished_at=UTC_TIMESTAMP() WHERE id=%s",
                    (row_count, len(content), job_id),
                )
    except asyncio.CancelledError:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE registry_property_annotation_exports SET status='interrupted',phase='finished',"
                    "error_code='service_stopped',finished_at=UTC_TIMESTAMP() WHERE id=%s", (job_id,),
                )
        raise
    except Exception as exc:  # noqa: BLE001 - return a safe, non-sensitive code
        _path(job_id).unlink(missing_ok=True)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE registry_property_annotation_exports SET status='failed',phase='finished',"
                    "error_code=%s,finished_at=UTC_TIMESTAMP() WHERE id=%s",
                    (type(exc).__name__[:60], job_id),
                )


async def get_export_job(job_id: str, requested_by: int | None = None) -> dict | None:
    try:
        job_id = str(UUID(job_id))
    except (ValueError, TypeError, AttributeError):
        return None
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await _delete_expired(cur)
            sql = (
                "SELECT id,status,phase,row_count,file_size,error_code,created_at,finished_at,expires_at "
                "FROM registry_property_annotation_exports WHERE id=%s AND expires_at>UTC_TIMESTAMP()"
            )
            params: tuple = (job_id,)
            if requested_by is not None:
                sql += " AND requested_by=%s"
                params += (int(requested_by),)
            await cur.execute(sql, params)
            job = _public(await cur.fetchone())
    if job and job["status"] == "ready" and not _path(job_id).is_file():
        job.update(status="expired", phase="finished", error_code="file_missing")
    return job


async def set_export_phase(job_id: str, phase: str) -> None:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE registry_property_annotation_exports SET phase=%s "
                "WHERE id=%s AND status='running'", (phase[:30], job_id),
            )


async def get_export_file(job_id: str, requested_by: int) -> Path | None:
    try:
        job_id = str(UUID(job_id))
    except (ValueError, TypeError, AttributeError):
        return None
    job = await get_export_job(job_id, requested_by)
    path = _path(job_id)
    return path if job and job["status"] == "ready" and path.is_file() else None


async def recover_interrupted_exports() -> int:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE registry_property_annotation_exports SET status='interrupted',phase='finished',"
                "error_code='service_restarted',finished_at=UTC_TIMESTAMP() "
                "WHERE status IN ('queued','running')"
            )
            count = int(cur.rowcount or 0)
            await _delete_expired(cur)
            return count


async def stop_annotation_export_tasks() -> None:
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
