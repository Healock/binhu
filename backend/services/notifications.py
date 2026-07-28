"""站内通知写入服务。"""

from database import db_manager


async def create_sync_failure_notifications(
    task_id: int,
    status: str,
    error_message: str | None,
) -> None:
    """为所有超级管理员创建一条自动同步失败通知。"""
    partial = status == "partial"
    title = "自动同步部分失败" if partial else "自动同步失败"
    summary = (error_message or "同步任务未正常完成").strip()
    content = f"同步任务 #{task_id}：{summary}"[:1000]

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
                    id, 'sync', %s, %s, %s, %s, UTC_TIMESTAMP()
                FROM _users
                WHERE role = 'super_admin'
                """,
                (
                    "warning" if partial else "error",
                    title,
                    content,
                    task_id,
                ),
            )
    finally:
        pool.release(conn)


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
