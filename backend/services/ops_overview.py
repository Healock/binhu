"""Combined read-only health information for the operations center."""

import os
from pathlib import Path

from config import settings
from database import db_manager
from services.backups import get_backup_schedule
from services.ops_client import get_container_overview
from services.ops_database import get_database_overview, get_mysql_status
from services.ops_redaction import redact_text


async def build_operations_overview() -> dict:
    try:
        container_data = await get_container_overview()
        containers = container_data.get("containers", [])
        container_error = None
    except Exception as exc:
        containers = []
        container_error = redact_text(str(exc))[:200]

    try:
        mysql_status = await get_mysql_status()
        databases = await get_database_overview()
    except Exception as exc:
        mysql_status = {
            "connected": False,
            "error": redact_text(str(exc))[:200],
        }
        databases = []

    try:
        backup_dir = Path(settings.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        disk_stat = os.statvfs(backup_dir)
        disk_total = disk_stat.f_frsize * disk_stat.f_blocks
        disk_free = disk_stat.f_frsize * disk_stat.f_bavail
        disk = {
            "total_bytes": disk_total,
            "free_bytes": disk_free,
            "used_bytes": disk_total - disk_free,
            "free_percent": round(
                disk_free / disk_total * 100,
                1,
            )
            if disk_total
            else 0,
        }
    except OSError as exc:
        disk = {
            "total_bytes": 0,
            "free_bytes": 0,
            "used_bytes": 0,
            "free_percent": 0,
            "error": redact_text(str(exc))[:200],
        }

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status, trigger_source, finished_at
                FROM _sync_log ORDER BY id DESC LIMIT 1
                """
            )
            sync_row = await cur.fetchone()
            await cur.execute(
                """
                SELECT id, status, finished_at, size_bytes
                FROM _backup_jobs ORDER BY id DESC LIMIT 1
                """
            )
            backup_row = await cur.fetchone()
            await cur.execute(
                """
                SELECT expires_at
                FROM _config_oauth_tokens ORDER BY id DESC LIMIT 1
                """
            )
            oauth_row = await cur.fetchone()
            await cur.execute("SELECT UTC_TIMESTAMP()")
            server_time = (await cur.fetchone())[0]
    finally:
        pool.release(conn)

    def iso(value):
        return value.isoformat() + "Z" if value else None

    oauth = {"configured": bool(oauth_row), "status": "not_configured"}
    if oauth_row:
        expires_at = oauth_row[0]
        if expires_at is None:
            oauth_status = "unknown"
        else:
            remaining = (expires_at - server_time).total_seconds()
            if remaining <= 0:
                oauth_status = "expired"
            elif remaining <= 7 * 24 * 60 * 60:
                oauth_status = "expiring"
            else:
                oauth_status = "healthy"
        oauth.update(
            {
                "status": oauth_status,
                "expires_at": iso(expires_at),
            }
        )

    return {
        "server_time": iso(server_time),
        "containers": containers,
        "container_error": container_error,
        "disk": disk,
        "mysql": mysql_status,
        "databases": databases,
        "latest_sync": {
            "id": sync_row[0],
            "status": sync_row[1],
            "trigger_source": sync_row[2],
            "finished_at": iso(sync_row[3]),
        }
        if sync_row
        else None,
        "latest_backup": {
            "id": backup_row[0],
            "status": backup_row[1],
            "finished_at": iso(backup_row[2]),
            "size_bytes": backup_row[3],
        }
        if backup_row
        else None,
        "backup_schedule": await get_backup_schedule(),
        "oauth": oauth,
    }
