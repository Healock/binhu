"""Periodic retention cleanup for venue registrations and photos."""

from __future__ import annotations

import asyncio
from pathlib import Path

from config import settings
from database import db_manager


async def cleanup_expired_venue_data() -> int:
    try:
        pool = db_manager.get_pool("registry")
    except ValueError:
        return 0
    conn = await pool.acquire()
    removed = 0
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id,storage_key FROM _venue_visit_photos WHERE retention_until<=UTC_TIMESTAMP() AND deleted_at IS NULL")
            rows = await cur.fetchall()
            root = Path(settings.VENUE_PHOTO_DIR).resolve()
            for visit_id, storage_key in rows:
                path = (root / str(storage_key)).resolve()
                if root in path.parents:
                    path.unlink(missing_ok=True)
                await cur.execute("UPDATE _venue_visit_photos SET deleted_at=UTC_TIMESTAMP() WHERE id=%s", (visit_id,))
                removed += 1
            await cur.execute("UPDATE _venue_visits SET deleted_at=UTC_TIMESTAMP() WHERE retention_until<=UTC_TIMESTAMP() AND deleted_at IS NULL")
        await conn.commit()
    finally:
        pool.release(conn)
    return removed


async def run_venue_cleanup_scheduler() -> None:
    while True:
        try:
            await cleanup_expired_venue_data()
        except Exception:
            pass
        await asyncio.sleep(3600)
