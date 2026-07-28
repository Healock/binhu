"""Lightweight in-process scheduler for the daily database backup."""

import asyncio

from services.backups import claim_due_backup_task, launch_backup_task


async def run_backup_scheduler() -> None:
    while True:
        try:
            task_id = await claim_due_backup_task()
            if task_id:
                print(f"[BACKUP] 已创建每日数据库备份任务: {task_id}")
                launch_backup_task(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[BACKUP] 定时检查失败: {exc}")
        await asyncio.sleep(30)
