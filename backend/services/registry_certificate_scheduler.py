"""Daily full-read scheduler for landlord responsibility notices."""

import asyncio

from services.registry_certificate_jobs import (
    claim_due_certificate_source_run,
    launch_certificate_source_run,
)


async def run_registry_certificate_scheduler() -> None:
    while True:
        try:
            run_id = await claim_due_certificate_source_run()
            if run_id:
                print(f"[REGISTRY_CERTIFICATE] 已创建每日全量读取任务: {run_id}")
                launch_certificate_source_run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[REGISTRY_CERTIFICATE] 定时检查失败: {type(exc).__name__}")
        await asyncio.sleep(60)

