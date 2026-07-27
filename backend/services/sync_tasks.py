"""手动与定时同步共用的任务创建、执行和排期逻辑。"""

import asyncio
from datetime import datetime

from database import db_manager
from services.notifications import create_sync_failure_notifications
from services.report_builders import BUILDERS
from services.sync_engine import SyncEngine


SYNC_TRIGGER_LOCK = "binhu_sync_trigger"
TERMINAL_STATUSES = {"success", "completed", "partial", "failed"}
_background_tasks: set[asyncio.Task] = set()


def _iso_utc(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


async def _acquire_trigger_lock(cur) -> bool:
    await cur.execute("SELECT GET_LOCK(%s, 5)", (SYNC_TRIGGER_LOCK,))
    row = await cur.fetchone()
    return bool(row and row[0] == 1)


async def _release_trigger_lock(cur) -> None:
    await cur.execute("SELECT RELEASE_LOCK(%s)", (SYNC_TRIGGER_LOCK,))
    await cur.fetchone()


async def _estimate_steps(cur) -> int:
    await cur.execute(
        "SELECT parser_type FROM _config_spreadsheets "
        "WHERE enabled=1 AND url<>'' AND file_id<>''"
    )
    parser_types = [row[0] for row in await cur.fetchall()]
    report_types = {ptype for ptype in parser_types if ptype in BUILDERS}
    return len(parser_types) + len(report_types) + (1 if report_types else 0)


async def create_sync_task(
    trigger_source: str,
    requested_by: int | None = None,
) -> tuple[int, str, str]:
    """创建同步任务；已有任务运行时返回 conflict。"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    task_id = 0
    try:
        async with conn.cursor() as cur:
            if not await _acquire_trigger_lock(cur):
                return 0, "conflict", "同步任务正在创建，请稍后再试"
            try:
                await cur.execute(
                    "SELECT id FROM _sync_log "
                    "WHERE status IN ('pending', 'running') LIMIT 1"
                )
                if await cur.fetchone():
                    return 0, "conflict", "已有同步任务正在运行，请等待完成"

                total_steps = await _estimate_steps(cur)
                await cur.execute(
                    """
                    INSERT INTO _sync_log (
                        status, trigger_source, requested_by, phase,
                        total_steps, completed_steps
                    ) VALUES ('pending', %s, %s, 'queued', %s, 0)
                    """,
                    (trigger_source, requested_by, total_steps),
                )
                task_id = cur.lastrowid
            finally:
                await _release_trigger_lock(cur)
    finally:
        pool.release(conn)

    launch_sync_task(task_id)
    return task_id, "pending", "同步任务已创建，正在后台执行"


async def claim_due_scheduled_task() -> int | None:
    """到点时原子领取一个自动同步任务。"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    task_id = None
    try:
        async with conn.cursor() as cur:
            if not await _acquire_trigger_lock(cur):
                return None
            try:
                await cur.execute(
                    "SELECT enabled, interval_minutes, next_run_at "
                    "FROM _sync_schedule WHERE id=1"
                )
                row = await cur.fetchone()
                if not row or not row[0] or row[2] is None:
                    return None

                await cur.execute("SELECT UTC_TIMESTAMP()")
                now = (await cur.fetchone())[0]
                if row[2] > now:
                    return None

                await cur.execute(
                    "SELECT id FROM _sync_log "
                    "WHERE status IN ('pending', 'running') LIMIT 1"
                )
                if await cur.fetchone():
                    return None

                total_steps = await _estimate_steps(cur)
                await cur.execute(
                    """
                    INSERT INTO _sync_log (
                        status, trigger_source, phase,
                        total_steps, completed_steps
                    ) VALUES ('pending', 'scheduled', 'queued', %s, 0)
                    """,
                    (total_steps,),
                )
                task_id = cur.lastrowid
                await cur.execute(
                    """
                    UPDATE _sync_schedule
                    SET last_triggered_at=UTC_TIMESTAMP(),
                        next_run_at=DATE_ADD(
                            UTC_TIMESTAMP(), INTERVAL interval_minutes MINUTE
                        )
                    WHERE id=1
                    """
                )
            finally:
                await _release_trigger_lock(cur)
    finally:
        pool.release(conn)

    return task_id


def launch_sync_task(task_id: int) -> None:
    task = asyncio.create_task(run_sync_task(task_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def stop_sync_tasks() -> None:
    """应用关闭时取消仍在本进程运行的同步任务。"""
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_sync_task(task_id: int) -> None:
    """执行任务，并在结束后重排下次同步和发送失败通知。"""
    try:
        engine = SyncEngine(db_manager.get_pool("online_data"))
        await engine.run_full_sync(task_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        pool = db_manager.get_pool("online_data")
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE _sync_log
                    SET status='failed', phase='finished',
                        current_item=NULL, error_message=%s,
                        finished_at=UTC_TIMESTAMP()
                    WHERE id=%s
                    """,
                    (str(exc)[:1000], task_id),
                )
        finally:
            pool.release(conn)

    task_status = await _get_task_terminal_state(task_id)
    if not task_status:
        return

    await reset_next_run_from_now()
    status, trigger_source, error_message = task_status
    if trigger_source == "scheduled" and status in {"partial", "failed"}:
        try:
            await create_sync_failure_notifications(
                task_id,
                status,
                error_message,
            )
        except Exception as exc:
            print(f"[SYNC] 站内通知写入失败: task={task_id} error={exc}")


async def _get_task_terminal_state(
    task_id: int,
) -> tuple[str, str, str | None] | None:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, trigger_source, error_message "
                "FROM _sync_log WHERE id=%s",
                (task_id,),
            )
            row = await cur.fetchone()
            if not row or row[0] not in TERMINAL_STATUSES:
                return None
            return row[0], row[1], row[2]
    finally:
        pool.release(conn)


async def reset_next_run_from_now() -> None:
    """任务结束后按当前配置重新计算下次执行时间。"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE _sync_schedule
                SET next_run_at = CASE
                    WHEN enabled=1 THEN DATE_ADD(
                        UTC_TIMESTAMP(), INTERVAL interval_minutes MINUTE
                    )
                    ELSE NULL
                END
                WHERE id=1
                """
            )
    finally:
        pool.release(conn)


async def get_schedule() -> dict:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT enabled, interval_minutes, next_run_at "
                "FROM _sync_schedule WHERE id=1"
            )
            row = await cur.fetchone()
            await cur.execute("SELECT UTC_TIMESTAMP()")
            server_time = (await cur.fetchone())[0]
        return {
            "enabled": bool(row[0]) if row else True,
            "interval_minutes": row[1] if row else 5,
            "next_run_at": _iso_utc(row[2]) if row else None,
            "server_time": _iso_utc(server_time),
        }
    finally:
        pool.release(conn)


async def update_schedule(
    enabled: bool,
    interval_minutes: int,
    updated_by: int,
) -> dict:
    if not 5 <= interval_minutes <= 10080:
        raise ValueError("同步间隔必须在5分钟到7天之间")

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE _sync_schedule
                SET enabled=%s,
                    interval_minutes=%s,
                    updated_by=%s,
                    next_run_at=CASE
                        WHEN %s=1 THEN DATE_ADD(
                            UTC_TIMESTAMP(), INTERVAL %s MINUTE
                        )
                        ELSE NULL
                    END
                WHERE id=1
                """,
                (
                    int(enabled),
                    interval_minutes,
                    updated_by,
                    int(enabled),
                    interval_minutes,
                ),
            )
    finally:
        pool.release(conn)
    return await get_schedule()


async def recover_interrupted_tasks() -> int:
    """启动时关闭遗留任务，并为中断的自动任务发送通知。"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    interrupted = []
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, trigger_source FROM _sync_log "
                "WHERE status IN ('pending', 'running')"
            )
            interrupted = list(await cur.fetchall())
            if interrupted:
                await cur.execute(
                    """
                    UPDATE _sync_log
                    SET status='failed', phase='finished',
                        current_item=NULL,
                        error_message='服务重启，同步任务中断',
                        finished_at=UTC_TIMESTAMP()
                    WHERE status IN ('pending', 'running')
                    """
                )
    finally:
        pool.release(conn)

    if interrupted:
        await reset_next_run_from_now()
    for task_id, trigger_source in interrupted:
        if trigger_source == "scheduled":
            try:
                await create_sync_failure_notifications(
                    task_id,
                    "failed",
                    "服务重启，同步任务中断",
                )
            except Exception as exc:
                print(f"[SYNC] 中断通知写入失败: task={task_id} error={exc}")
    return len(interrupted)
