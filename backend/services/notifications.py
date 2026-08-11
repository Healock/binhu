"""站内通知写入服务。"""

import re

from database import db_manager


SYNC_FAILURE_STATUSES = {"partial", "failed"}
SYNC_SUCCESS_STATUSES = {"success", "completed"}


def normalize_sync_error(error_message: str | None) -> str:
    """移除易变内容并稳定错误顺序，用于连续自动同步通知去重。"""
    lines = []
    for raw_line in (error_message or "同步任务未正常完成").splitlines():
        line = re.sub(r"同步任务\s*#\d+", "同步任务#", raw_line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(sorted(set(lines)))


async def create_sync_status_notifications(
    task_id: int,
    status: str,
    error_message: str | None,
) -> bool:
    """发送首次失败、错误变化或首次恢复通知。"""
    if status not in SYNC_FAILURE_STATUSES | SYNC_SUCCESS_STATUSES:
        return False

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, error_message FROM _sync_log "
                "WHERE trigger_source='scheduled' AND id<%s "
                "AND status IN ('success', 'completed', 'partial', 'failed') "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            previous = await cur.fetchone()

            if status in SYNC_FAILURE_STATUSES:
                if (
                    previous
                    and previous[0] in SYNC_FAILURE_STATUSES
                    and normalize_sync_error(previous[1])
                    == normalize_sync_error(error_message)
                ):
                    return False
                partial = status == "partial"
                severity = "warning" if partial else "error"
                title = "自动同步部分失败" if partial else "自动同步失败"
                summary = (error_message or "同步任务未正常完成").strip()
                content = f"同步任务 #{task_id}：{summary}"[:1000]
            else:
                if not previous or previous[0] not in SYNC_FAILURE_STATUSES:
                    return False
                severity = "success"
                title = "自动同步已恢复"
                content = f"同步任务 #{task_id} 已正常完成，自动同步已经恢复。"

            await cur.execute(
                """
                INSERT IGNORE INTO _notifications (
                    user_id, category, severity, title, content,
                    related_task_id, created_at
                )
                SELECT
                    id, 'sync', %s, %s, %s, %s, UTC_TIMESTAMP()
                FROM _users
                WHERE role = 'super_admin'
                """,
                (
                    severity,
                    title,
                    content,
                    task_id,
                ),
            )
            return True
    finally:
        pool.release(conn)


async def create_sync_failure_notifications(
    task_id: int,
    status: str,
    error_message: str | None,
) -> bool:
    """兼容启动恢复等旧调用方，并复用自动同步通知去重。"""
    return await create_sync_status_notifications(task_id, status, error_message)


async def create_backup_failure_notifications(
    task_id: int,
    error_message: str | None,
) -> None:
    """Notify every super administrator when a database backup fails."""
    summary = (error_message or "数据库备份未正常完成").strip()
    content = f"数据库备份任务 #{task_id}：{summary}"[:1000]

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT IGNORE INTO _notifications (
                    user_id, category, severity, title, content,
                    related_task_id, created_at
                )
                SELECT
                    id, 'backup', 'error', '数据库备份失败', %s, %s,
                    UTC_TIMESTAMP()
                FROM _users
                WHERE role = 'super_admin'
                """,
                (content, task_id),
            )
    finally:
        pool.release(conn)
