"""手动与定时同步共用的任务创建、执行和排期逻辑。"""

import asyncio
import inspect
import json
from datetime import datetime

from config import settings
from database import db_manager
from services.notifications import (
    create_sync_failure_notifications,
    create_sync_status_notifications,
)
from services.report_builders import BUILDERS
from services.sync_engine import SyncEngine


SYNC_TRIGGER_LOCK = "binhu_sync_trigger"
TERMINAL_STATUSES = {"success", "completed", "partial", "failed"}
DEFAULT_SYNC_INTERVAL_MINUTES = 10
_background_tasks: set[asyncio.Task] = set()
QMF_TERMINAL_STATUSES = {"success", "warning", "failed", "interrupted"}


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
    return (
        len(parser_types)
        + len(report_types)
        + (1 if report_types else 0)
    )


async def run_scheduled_visit_source_acquisition() -> None:
    """Run the optional source preview only when explicitly configured.

    This is deliberately isolated from SyncEngine: a source failure is stored
    as a failed preview run and never changes the current visit projection.
    """
    from config import settings
    from services.business_time import get_business_date
    from services.visit_source import VisitSourceError, commit_rows, fetch_rows
    from services.visit_import import VISIT_IMPORT_LOCK_NAME

    if not settings.VISIT_SOURCE_AUTO_CONFIRM:
        return
    if not settings.VISIT_SOURCE_MOCK and not settings.VISIT_SOURCE_BASE_URL:
        return
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT GET_LOCK(%s, 0)", (VISIT_IMPORT_LOCK_NAME,))
            lock_row = await cur.fetchone()
        if not lock_row or lock_row[0] != 1:
            return
        async with conn.cursor() as cur:
            business_date = await get_business_date(cur)
        for source in ("detail", "rating"):
            try:
                result = await fetch_rows(source, business_date, business_date)
                status = "pending_confirmation"
                error_code = None
                error_message = None
            except VisitSourceError as exc:
                result = {"rows": [], "record_count": 0, "valid_count": 0, "issue_count": 1, "issues": [exc.message]}
                status = "failed"
                error_code = exc.code
                error_message = exc.message

            # Close the fetch/insert cursor before invoking the existing importers;
            # they manage their own transaction and commit the projection atomically.
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO _visit_source_runs (
                        source_kind, trigger_source, status, requested_start_date,
                        requested_end_date, response_business_date, source_page,
                        record_count, valid_count, issue_count, summary_json,
                        payload_json, error_code, error_message
                    ) VALUES (%s, 'scheduled', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        source,
                        status,
                        business_date,
                        business_date,
                        result.get("response_business_date"),
                        "走访明细" if source == "detail" else "新星级评分管理",
                        result.get("record_count", 0),
                        result.get("valid_count", 0),
                        result.get("issue_count", 0),
                        json.dumps({"issues": result.get("issues", [])}, ensure_ascii=False),
                        json.dumps(result.get("rows", []), ensure_ascii=False, default=str) if result.get("rows") else None,
                        error_code,
                        error_message,
                    ),
                )
                run_id = int(cur.lastrowid)
            if status != "pending_confirmation":
                await conn.commit()
                continue
            try:
                import_result = await commit_rows(
                    conn,
                    kind=source,
                    rows=result["rows"],
                    start_date=business_date,
                    user_id=0,
                    source_type="scheduled_source",
                    source_run_id=run_id,
                )
                final_status = "confirmed" if import_result.get("status") in ("success", "partial") else "failed"
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE _visit_source_runs SET status=%s, summary_json=%s, confirmed_at=UTC_TIMESTAMP() WHERE id=%s",
                        (final_status, json.dumps({"batch_id": import_result.get("batch_id"), "import_status": import_result.get("status")}, ensure_ascii=False), run_id),
                    )
                await conn.commit()
            except Exception as exc:
                await conn.rollback()
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE _visit_source_runs SET status='failed', error_code='commit_failed', error_message=%s WHERE id=%s",
                        (str(exc)[:500], run_id),
                    )
                await conn.commit()
    finally:
        async with conn.cursor() as cur:
            await cur.execute("SELECT RELEASE_LOCK(%s)", (VISIT_IMPORT_LOCK_NAME,))
        pool.release(conn)


async def _scheduled_qmf_business_date() -> str:
    from services.business_time import get_business_date

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            value = await get_business_date(cur)
        return value.isoformat()
    finally:
        pool.release(conn)


async def run_scheduled_qmf_source_acquisition() -> dict | None:
    """在每日第一次自动同步前获取模型三，并等待结果可供日报使用。

    失败只记录在独立来源任务中，不阻断腾讯在线表同步；同一业务日不重复
    自动请求，人工触发仍可单独执行。
    """
    from services.external_acquisition_jobs import create_job, get_job

    business_date = await _scheduled_qmf_business_date()
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id,status,payload_json FROM _external_acquisition_runs "
                "WHERE kind='qmf_source' ORDER BY id DESC LIMIT 30"
            )
            rows = await cur.fetchall()
    finally:
        pool.release(conn)

    for run_id, status, payload in rows:
        try:
            payload_data = json.loads(payload) if isinstance(payload, str) else (payload or {})
        except (TypeError, ValueError):
            payload_data = {}
        if payload_data.get("trigger") != "scheduled":
            continue
        if payload_data.get("business_date") != business_date:
            continue
        if str(status) in QMF_TERMINAL_STATUSES:
            return await get_job(int(run_id))

    from services.qmf_source_sync import run_qmf_source_sync

    job, _ = await create_job(
        "qmf_source",
        None,
        {
            "source": "legacy-model-three",
            "mode": "pending-only",
            "trigger": "scheduled",
            "business_date": business_date,
        },
        run_qmf_source_sync,
        dedupe_key=f"scheduled:{business_date}",
    )
    run_id = int(job.get("id") or 0)
    if not run_id:
        return job

    # 来源客户端自带超时保护；这里再设置上限，避免外部平台异常拖住主同步。
    for _ in range(360):
        current = await get_job(run_id)
        if not current or current.get("status") not in {"queued", "running"}:
            return current or job
        await asyncio.sleep(0.5)
    return await get_job(run_id) or job


async def create_sync_task(
    trigger_source: str,
    requested_by: int | None = None,
) -> tuple[int, str, str]:
    """创建同步任务；已有任务运行时返回 conflict。"""
    from services.local_source import local_data_source_enabled
    if local_data_source_enabled():
        return 0, "disabled", "腾讯数据源已下线，无需创建同步任务"
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
    from services.local_source import local_data_source_enabled

    # Local data is authoritative after the cutover.  Do not even create a
    # legacy scheduled-sync row; otherwise the task queue would show a fake
    # Tencent synchronization job that can never run.
    if local_data_source_enabled():
        return None
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
    from services.local_source import local_data_source_enabled
    if local_data_source_enabled():
        return
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
    from services.local_source import local_data_source_enabled
    if local_data_source_enabled():
        pool = db_manager.get_pool("online_data")
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE _sync_log
                    SET status='failed', phase='finished', current_item=NULL,
                        error_message='腾讯数据源已下线，旧同步任务已停用',
                        finished_at=UTC_TIMESTAMP()
                    WHERE id=%s AND status IN ('pending','running')
                    """,
                    (task_id,),
                )
        finally:
            pool.release(conn)
        return
    try:
        trigger_source = await _get_task_trigger_source(task_id)
        if trigger_source == "scheduled":
            try:
                await run_scheduled_qmf_source_acquisition()
            except Exception as exc:
                # 模型三来源独立失败，不阻断在线表同步；总汇总会给出
                # “独立来源尚未完成”的明确原因。
                print(f"[QMF_SOURCE] scheduled acquisition failed: {type(exc).__name__}")
        engine = SyncEngine(db_manager.get_pool("online_data"))
        await engine.run_full_sync(task_id)
        task_state = await _get_task_terminal_state(task_id)
        if task_state and task_state[1] == "scheduled":
            try:
                await run_scheduled_visit_source_acquisition()
            except Exception as exc:
                # Source acquisition is isolated from the legacy sync result.
                print(f"[VISIT_SOURCE] scheduled acquisition failed: {type(exc).__name__}")
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
    if trigger_source == "scheduled":
        try:
            await create_sync_status_notifications(
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


async def _get_task_trigger_source(task_id: int) -> str | None:
    pool = db_manager.get_pool("online_data")
    acquired = pool.acquire()
    if not inspect.isawaitable(acquired):
        return None
    conn = await acquired
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT trigger_source FROM _sync_log WHERE id=%s",
                (task_id,),
            )
            row = await cur.fetchone()
            return str(row[0]) if row and row[0] else None
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
            "interval_minutes": row[1] if row else DEFAULT_SYNC_INTERVAL_MINUTES,
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
