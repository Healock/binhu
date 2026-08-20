"""在线状态旧记录清理任务。"""

import asyncio
from contextlib import suppress

from database import db_manager


async def cleanup_presence_clients() -> int:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM _user_presence_clients "
                "WHERE last_seen_at < UTC_TIMESTAMP() - INTERVAL 24 HOUR"
            )
            return int(cur.rowcount or 0)
    finally:
        pool.release(conn)


async def run_presence_cleanup_scheduler() -> None:
    while True:
        with suppress(Exception):
            await cleanup_presence_clients()
        await asyncio.sleep(3600)

