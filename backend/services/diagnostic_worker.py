"""独立诊断 Worker：Redis Stream 快速唤醒，MySQL 队列可靠兜底。"""

from __future__ import annotations

import asyncio
import signal

from database import db_manager
from services.diagnostics import cleanup_expired, execute_job


async def _queued_jobs(limit: int = 20) -> list[str]:
    pool = db_manager.get_pool("platform")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT job_id FROM diagnostic_jobs WHERE status='queued' ORDER BY queued_at, created_at LIMIT %s",
                (limit,),
            )
            return [str(row[0]) for row in await cur.fetchall()]
    finally:
        pool.release(conn)


async def run_worker() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig:
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
    last_cleanup = 0.0
    while not stop.is_set():
        try:
            for job_id in await _queued_jobs():
                await execute_job(job_id)
            now = loop.time()
            if now - last_cleanup > 3600:
                await cleanup_expired()
                last_cleanup = now
        except Exception as exc:
            print(f"[DIAGNOSTIC] worker loop unavailable: {type(exc).__name__}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            continue


def main() -> None:
    asyncio.run(_main())


async def _main() -> None:
    await db_manager.init_all()
    try:
        await run_worker()
    finally:
        await db_manager.close_all()


if __name__ == "__main__":
    main()
