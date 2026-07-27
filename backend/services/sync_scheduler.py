"""进程内定时同步循环，领取任务时由数据库锁防止重复。"""

import asyncio

from services.sync_tasks import claim_due_scheduled_task, launch_sync_task


async def run_sync_scheduler() -> None:
    while True:
        try:
            task_id = await claim_due_scheduled_task()
            if task_id:
                print(f"[SCHEDULER] 已创建自动同步任务: {task_id}")
                launch_sync_task(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[SCHEDULER] 检查失败: {exc}")
        await asyncio.sleep(5)
