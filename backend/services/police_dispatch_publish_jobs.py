"""Persistent task lifecycle for background police dispatch publishing."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable

from database import db_manager


_background_tasks: set[asyncio.Task] = set()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _public_run(row: tuple | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": int(row[0]),
        "batch_id": int(row[1]),
        "status": str(row[2]),
        "phase": str(row[3]),
        "total_count": int(row[4] or 0),
        "processed_count": int(row[5] or 0),
        "success_count": int(row[6] or 0),
        "conflict_count": int(row[7] or 0),
        "reconciliation_count": int(row[8] or 0),
        "retryable_count": int(row[9] or 0),
        "error_code": str(row[10] or ""),
        "error_message": str(row[11] or ""),
        "started_at": _iso(row[12]),
        "finished_at": _iso(row[13]),
        "created_at": _iso(row[14]),
        "updated_at": _iso(row[15]),
    }


RUN_SELECT = """
    SELECT id,batch_id,status,phase,total_count,processed_count,
           success_count,conflict_count,reconciliation_count,retryable_count,
           error_code,error_message,started_at,finished_at,created_at,updated_at
    FROM _police_dispatch_publish_runs
"""


async def get_police_publish_run(run_id: int) -> dict[str, Any] | None:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"{RUN_SELECT} WHERE id=%s", (run_id,))
            return _public_run(await cur.fetchone())


async def get_latest_police_publish_run(batch_id: int) -> dict[str, Any] | None:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"{RUN_SELECT} WHERE batch_id=%s ORDER BY id DESC LIMIT 1",
                (batch_id,),
            )
            return _public_run(await cur.fetchone())


def launch_police_publish_run(
    run_id: int,
    runner: Callable[[int], Awaitable[None]],
) -> None:
    task = asyncio.create_task(runner(run_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def stop_police_publish_tasks() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def recover_interrupted_police_publish_runs() -> int:
    """Freeze possibly sent chunks and release only rows that were never sent."""
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id,batch_id FROM _police_dispatch_publish_runs "
                "WHERE status IN ('pending','running') ORDER BY id"
            )
            runs = [(int(row[0]), int(row[1])) for row in await cur.fetchall()]
            for run_id, batch_id in runs:
                await cur.execute("""
                    UPDATE _police_dispatch_publish_run_items AS item
                    JOIN _police_dispatch_tasks AS task ON task.id=item.task_id
                    SET item.status=CASE task.publish_status
                            WHEN 'success' THEN 'success'
                            WHEN 'conflict' THEN 'conflict'
                            WHEN 'needs_reconciliation' THEN 'needs_reconciliation'
                            WHEN 'retryable' THEN 'retryable'
                            ELSE item.status END
                    WHERE item.run_id=%s
                      AND task.publish_status IN (
                          'success','conflict','needs_reconciliation','retryable'
                      )
                """, (run_id,))
                await cur.execute("""
                    UPDATE _police_dispatch_tasks AS task
                    JOIN _police_dispatch_publish_run_items AS item
                      ON item.task_id=task.id AND item.run_id=%s
                    SET task.publish_status='needs_reconciliation',
                        task.task_status='publish_failed',
                        task.publish_error='服务重启时腾讯请求可能已经送达，等待同步对账',
                        task.version=task.version+1
                    WHERE item.status='sending' AND task.publish_status='publishing'
                """, (run_id,))
                await cur.execute("""
                    UPDATE _police_dispatch_publish_run_items
                    SET status='needs_reconciliation',error_code='service_restarted_uncertain'
                    WHERE run_id=%s AND status='sending'
                """, (run_id,))
                await cur.execute("""
                    UPDATE _police_dispatch_tasks AS task
                    JOIN _police_dispatch_publish_run_items AS item
                      ON item.task_id=task.id AND item.run_id=%s
                    SET task.publish_status='retryable',task.task_status='pending_publish',
                        task.publish_error='服务重启前尚未向腾讯发送，可安全重试',
                        task.version=task.version+1
                    WHERE item.status IN ('queued','checking')
                      AND task.publish_status='publishing'
                """, (run_id,))
                await cur.execute("""
                    UPDATE _police_dispatch_publish_run_items
                    SET status='retryable',error_code='service_restarted_safe'
                    WHERE run_id=%s AND status IN ('queued','checking')
                """, (run_id,))
                await cur.execute("""
                    UPDATE _police_dispatch_publish_runs AS run SET
                        status='failed',phase='finished',
                        processed_count=(SELECT COUNT(*) FROM _police_dispatch_publish_run_items
                                         WHERE run_id=run.id AND status<>'queued'),
                        success_count=(SELECT COUNT(*) FROM _police_dispatch_publish_run_items
                                       WHERE run_id=run.id AND status='success'),
                        conflict_count=(SELECT COUNT(*) FROM _police_dispatch_publish_run_items
                                        WHERE run_id=run.id AND status='conflict'),
                        reconciliation_count=(SELECT COUNT(*) FROM _police_dispatch_publish_run_items
                                              WHERE run_id=run.id AND status='needs_reconciliation'),
                        retryable_count=(SELECT COUNT(*) FROM _police_dispatch_publish_run_items
                                         WHERE run_id=run.id AND status='retryable'),
                        error_code='service_restarted',
                        error_message='服务重启，未发送任务可重试，可能已发送任务等待同步对账',
                        finished_at=UTC_TIMESTAMP()
                    WHERE run.id=%s
                """, (run_id,))
                await cur.execute("""
                    UPDATE _police_dispatch_batches AS batch SET
                        status=CASE
                            WHEN EXISTS (
                                SELECT 1 FROM _police_dispatch_tasks task
                                WHERE task.batch_id=batch.id
                                  AND task.task_status='pending_review'
                            ) THEN 'reviewing'
                            WHEN EXISTS (
                                SELECT 1 FROM _police_dispatch_tasks task
                                WHERE task.batch_id=batch.id
                                  AND task.publish_status IN ('needs_reconciliation','conflict')
                            ) THEN 'reconciling'
                            WHEN EXISTS (
                                SELECT 1 FROM _police_dispatch_tasks task
                                WHERE task.batch_id=batch.id
                                  AND task.publish_status IN ('pending','publishing','retryable')
                            ) THEN 'ready_to_publish'
                            ELSE 'completed' END,
                        last_error='服务重启，未发送任务可重试，可能已发送任务等待同步对账',
                        completed_at=NULL
                    WHERE batch.id=%s
                """, (batch_id,))
            return len(runs)
