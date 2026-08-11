"""工单临期、超期提醒与附件到期清理。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date, datetime, timedelta

from database import db_manager
from services.online_source import json_value
from services.workflow_support import queue_user_ids, remove_attachment, workflow_notification
from services.photo_sheet_sync import run_photo_sheet_maintenance_once
from config import settings


async def run_workflow_maintenance_once() -> dict:
    if not settings.WORKFLOW_FEATURE_ENABLED:
        return {"reminders": 0, "attachments_deleted": 0, "orphan_files_checked": 0}
    try:
        pool = db_manager.get_pool("workflow")
    except ValueError:
        return {"reminders": 0, "attachments_deleted": 0, "orphan_files_checked": 0}
    conn = await pool.acquire()
    reminders = 0
    deleted = 0
    orphaned = 0
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT order_row.id, order_row.current_assignee_user_id, order_row.current_queue, "
                "order_row.due_at, order_row.version_no, definition.config_json "
                "FROM work_orders order_row "
                "LEFT JOIN work_order_steps step ON step.work_order_id=order_row.id "
                "AND step.status IN ('queued','in_progress','pending_requester') "
                "LEFT JOIN workflow_steps definition ON definition.id=step.workflow_step_id "
                "WHERE order_row.status IN ('queued','in_progress','pending_requester') "
                "AND order_row.due_at IS NOT NULL"
            )
            now = datetime.utcnow()
            today = date.today()
            for ticket_id, assignee_id, queue, due_at, version_no, raw_config in await cur.fetchall():
                config = json_value(raw_config, {})
                before_minutes = int(config.get("reminder_before_minutes") or 60)
                reminder_type = ""
                reminder_date = None
                severity = "info"
                if due_at <= now:
                    reminder_type = "overdue"
                    reminder_date = today
                    severity = "warning"
                elif due_at <= now + timedelta(minutes=before_minutes):
                    reminder_type = "due_soon"
                    reminder_date = due_at.date()
                if not reminder_type:
                    continue
                recipients = [int(assignee_id)] if assignee_id else await queue_user_ids(cur, str(queue or ""))
                for recipient in recipients:
                    await cur.execute(
                        "INSERT IGNORE INTO work_order_reminders "
                        "(work_order_id, reminder_type, reminder_date, recipient_user_id) VALUES (%s,%s,%s,%s)",
                        (ticket_id, reminder_type, reminder_date, recipient),
                    )
                    if cur.rowcount != 1:
                        continue
                    reminders += 1
                    await workflow_notification(
                        cur, user_ids=[recipient], ticket_id=int(ticket_id),
                        event_key=f"{reminder_type}_{version_no}_{reminder_date}",
                        title="工单已超期" if reminder_type == "overdue" else "工单即将到期",
                        content=f"工单 #{ticket_id} 请及时处理。", severity=severity,
                    )

            await cur.execute(
                "SELECT attachment.id, attachment.storage_key "
                "FROM work_order_attachments attachment "
                "JOIN work_orders order_row ON order_row.id=attachment.work_order_id "
                "WHERE attachment.deleted_at IS NULL AND order_row.completed_at IS NOT NULL "
                "AND order_row.completed_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 90 DAY)"
            )
            for attachment_id, storage_key in await cur.fetchall():
                remove_attachment(str(storage_key))
                await cur.execute(
                    "UPDATE work_order_attachments SET deleted_at=UTC_TIMESTAMP(), retention_until=UTC_TIMESTAMP() "
                    "WHERE id=%s AND deleted_at IS NULL",
                    (attachment_id,),
                )
                deleted += int(cur.rowcount or 0)
            # 接口先提交删除元数据，再删除物理文件。若进程在两步之间中断，
            # 调度器会幂等清理受保护目录中的残留文件。
            await cur.execute(
                "SELECT storage_key FROM work_order_attachments "
                "WHERE deleted_at IS NOT NULL ORDER BY deleted_at LIMIT 500"
            )
            for (storage_key,) in await cur.fetchall():
                remove_attachment(str(storage_key))
                orphaned += 1
    finally:
        pool.release(conn)
    return {"reminders": reminders, "attachments_deleted": deleted, "orphan_files_checked": orphaned}


async def run_workflow_scheduler() -> None:
    while True:
        with suppress(Exception):
            await run_workflow_maintenance_once()
        with suppress(Exception):
            await run_photo_sheet_maintenance_once()
        await asyncio.sleep(300)
