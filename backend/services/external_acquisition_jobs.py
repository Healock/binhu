"""可追踪的外部数据获取后台任务。

任务记录只保存来源、阶段、数量和安全错误摘要，不保存外部行正文。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Awaitable, Callable

from database import db_manager

_background_tasks: set[asyncio.Task] = set()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _safe_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _public(row: tuple | None) -> dict[str, Any] | None:
    if not row:
        return None
    result = {
        "id": int(row[0]),
        "kind": str(row[1]),
        "status": str(row[2]),
        "phase": str(row[3] or "queued"),
        "current": int(row[4] or 0),
        "total": int(row[5]) if row[5] is not None else None,
        "message": str(row[6] or ""),
        "requested_by": int(row[7]) if row[7] is not None else None,
        "result": json.loads(row[8]) if isinstance(row[8], str) and row[8] else (row[8] or {}),
        "error_code": str(row[9]) if row[9] else None,
        "error_message": str(row[10]) if row[10] else None,
        "created_at": _iso(row[11]),
        "started_at": _iso(row[12]),
        "finished_at": _iso(row[13]),
        "updated_at": _iso(row[14]),
    }
    if result["total"]:
        result["progress"] = min(100, round(result["current"] * 100 / result["total"]))
    else:
        result["progress"] = None
    return result


SELECT = """
SELECT id,kind,status,phase,current_count,total_count,progress_message,
       requested_by,result_json,error_code,error_message,created_at,started_at,
       finished_at,updated_at
FROM _external_acquisition_runs
"""


class JobContext:
    def __init__(self, run_id: int):
        self.run_id = run_id

    async def update(self, *, phase: str, current: int | None = None,
                     total: int | None = None, message: str = "") -> None:
        pool = db_manager.get_pool("online_data")
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE _external_acquisition_runs SET phase=%s,current_count=%s,total_count=%s,"
                    "progress_message=%s,status='running' WHERE id=%s",
                    (phase, current, total, message[:500], self.run_id),
                )


async def create_job(kind: str, requested_by: int | None, payload: dict[str, Any],
                     runner: Callable[[JobContext], Awaitable[dict[str, Any]]],
                     *, dedupe_key: str | None = None) -> tuple[dict[str, Any], bool]:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            if dedupe_key:
                await cur.execute(
                    "SELECT id FROM _external_acquisition_runs WHERE kind=%s AND dedupe_key=%s "
                    "AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
                    (kind, dedupe_key),
                )
                existing = await cur.fetchone()
                if existing:
                    run_id = int(existing[0])
                    await conn.commit()
                    return ((await get_job(run_id)) or {}, True)
            await cur.execute(
                "INSERT INTO _external_acquisition_runs (kind,status,phase,requested_by,payload_json,dedupe_key) "
                "VALUES (%s,'queued','queued',%s,%s,%s)",
                (kind, requested_by, _safe_json(payload), dedupe_key),
            )
            run_id = int(cur.lastrowid)
            await conn.commit()
    task = asyncio.create_task(_execute(run_id, runner))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return ((await get_job(run_id)) or {}, False)


async def _fetch_row(pool, run_id: int) -> tuple | None:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"{SELECT} WHERE id=%s", (run_id,))
            return await cur.fetchone()


async def get_job(run_id: int) -> dict[str, Any] | None:
    return _public(await _fetch_row(db_manager.get_pool("online_data"), run_id))


async def latest_job(kind: str) -> dict[str, Any] | None:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"{SELECT} WHERE kind=%s ORDER BY id DESC LIMIT 1", (kind,))
            return _public(await cur.fetchone())


async def _execute(run_id: int, runner: Callable[[JobContext], Awaitable[dict[str, Any]]]) -> None:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _external_acquisition_runs SET status='running',phase='starting',started_at=UTC_TIMESTAMP() WHERE id=%s",
                (run_id,),
            )
    try:
        result = await runner(JobContext(run_id))
    except asyncio.CancelledError:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE _external_acquisition_runs SET status='interrupted',phase='interrupted',"
                    "error_code='cancelled',error_message='服务关闭或任务被取消',finished_at=UTC_TIMESTAMP() WHERE id=%s",
                    (run_id,),
                )
        raise
    except Exception as exc:  # noqa: BLE001 - safe public summary
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE _external_acquisition_runs SET status='failed',phase='finished',"
                    "error_code=%s,error_message=%s,finished_at=UTC_TIMESTAMP() WHERE id=%s",
                    (type(exc).__name__[:60], str(exc)[:500], run_id),
                )
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            status = str(result.pop("status", "success"))
            await cur.execute(
                "UPDATE _external_acquisition_runs SET status=%s,phase='finished',result_json=%s,"
                "progress_message=%s,finished_at=UTC_TIMESTAMP() WHERE id=%s",
                (status, _safe_json(result), str(result.get("message", "已完成"))[:500], run_id),
            )


async def recover_interrupted_jobs() -> int:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _external_acquisition_runs SET status='interrupted',phase='interrupted',"
                "error_code='service_restarted',error_message='服务重启，任务未继续执行',finished_at=UTC_TIMESTAMP() "
                "WHERE status IN ('queued','running')"
            )
            return int(cur.rowcount or 0)


async def stop_external_acquisition_tasks() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
