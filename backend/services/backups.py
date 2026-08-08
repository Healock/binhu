"""Daily and manual eight-database backups with atomic files and retention."""

import asyncio
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from config import settings
from database import db_manager
from services.business_time import get_business_timezone_name, resolve_timezone
from services.notifications import create_backup_failure_notifications
from services.ops_redaction import redact_text


BACKUP_LOCK = "binhu_backup_trigger"
BACKUP_DATABASES = (
    settings.MYSQL_ONLINE_DATA_DB,
    settings.MYSQL_ARCHIVE_DB,
    settings.MYSQL_DAILY_REPORT_DB,
    settings.MYSQL_PLATFORM_DB,
    settings.MYSQL_VISIT_DB,
    settings.MYSQL_DISPATCH_DB,
    settings.MYSQL_REGISTRY_DB,
    settings.MYSQL_WORKFLOW_DB,
)
PLATFORM_FILENAME = re.compile(
    r"^binhu-db-\d{8}T\d{6}Z-job\d+\.sql\.gz$"
)
_background_tasks: set[asyncio.Task] = set()


def _iso_utc(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def calculate_next_run_utc(
    now_utc: datetime,
    timezone_name: str,
    run_hour: int,
    run_minute: int,
) -> datetime:
    """Return the next configured local wall-clock time as naive UTC."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(resolve_timezone(timezone_name))
    candidate = local_now.replace(
        hour=run_hour,
        minute=run_minute,
        second=0,
        microsecond=0,
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


async def _acquire_lock(cur) -> bool:
    await cur.execute("SELECT GET_LOCK(%s, 5)", (BACKUP_LOCK,))
    row = await cur.fetchone()
    return bool(row and row[0] == 1)


async def _release_lock(cur) -> None:
    await cur.execute("SELECT RELEASE_LOCK(%s)", (BACKUP_LOCK,))
    await cur.fetchone()


async def get_backup_schedule() -> dict:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT enabled, run_hour, run_minute, retention_days,
                       next_run_at, last_triggered_at
                FROM _backup_schedule WHERE id=1
                """
            )
            row = await cur.fetchone()
            await cur.execute("SELECT UTC_TIMESTAMP()")
            now = (await cur.fetchone())[0]
            if row and row[0] and row[4] is None:
                timezone_name = await get_business_timezone_name(cur)
                next_run = calculate_next_run_utc(
                    now,
                    timezone_name,
                    row[1],
                    row[2],
                )
                await cur.execute(
                    "UPDATE _backup_schedule SET next_run_at=%s WHERE id=1",
                    (next_run,),
                )
                row = (*row[:4], next_run, row[5])
        return {
            "enabled": bool(row[0]) if row else True,
            "run_hour": row[1] if row else 2,
            "run_minute": row[2] if row else 0,
            "retention_days": row[3] if row else 7,
            "next_run_at": _iso_utc(row[4]) if row else None,
            "last_triggered_at": _iso_utc(row[5]) if row else None,
            "server_time": _iso_utc(now),
        }
    finally:
        pool.release(conn)


async def update_backup_schedule(
    enabled: bool,
    run_hour: int,
    run_minute: int,
    updated_by: int,
) -> dict:
    if not 0 <= run_hour <= 23 or not 0 <= run_minute <= 59:
        raise ValueError("备份时间无效")

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT UTC_TIMESTAMP()")
            now = (await cur.fetchone())[0]
            timezone_name = await get_business_timezone_name(cur)
            next_run = (
                calculate_next_run_utc(
                    now,
                    timezone_name,
                    run_hour,
                    run_minute,
                )
                if enabled
                else None
            )
            await cur.execute(
                """
                UPDATE _backup_schedule
                SET enabled=%s, run_hour=%s, run_minute=%s,
                    retention_days=7, next_run_at=%s, updated_by=%s
                WHERE id=1
                """,
                (
                    int(enabled),
                    run_hour,
                    run_minute,
                    next_run,
                    updated_by,
                ),
            )
    finally:
        pool.release(conn)
    return await get_backup_schedule()


async def create_backup_task(
    trigger_source: str,
    requested_by: int | None = None,
) -> tuple[int, str, str]:
    if trigger_source not in {"manual", "scheduled"}:
        raise ValueError("未知备份来源")

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    task_id = 0
    try:
        async with conn.cursor() as cur:
            if not await _acquire_lock(cur):
                return 0, "conflict", "备份任务正在创建，请稍后重试"
            try:
                await cur.execute(
                    "SELECT id FROM _backup_jobs "
                    "WHERE status IN ('pending', 'running') LIMIT 1"
                )
                if await cur.fetchone():
                    return 0, "conflict", "已有数据库备份正在运行"
                await cur.execute(
                    """
                    INSERT INTO _backup_jobs (
                        trigger_source, status, requested_by
                    ) VALUES (%s, 'pending', %s)
                    """,
                    (trigger_source, requested_by),
                )
                task_id = cur.lastrowid
            finally:
                await _release_lock(cur)
    finally:
        pool.release(conn)

    launch_backup_task(task_id)
    return task_id, "pending", "数据库备份已开始"


async def claim_due_backup_task() -> int | None:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    task_id = None
    try:
        async with conn.cursor() as cur:
            if not await _acquire_lock(cur):
                return None
            try:
                await cur.execute(
                    """
                    SELECT enabled, run_hour, run_minute, next_run_at
                    FROM _backup_schedule WHERE id=1
                    """
                )
                row = await cur.fetchone()
                if not row or not row[0]:
                    return None

                await cur.execute("SELECT UTC_TIMESTAMP()")
                now = (await cur.fetchone())[0]
                timezone_name = await get_business_timezone_name(cur)
                if row[3] is None:
                    next_run = calculate_next_run_utc(
                        now,
                        timezone_name,
                        row[1],
                        row[2],
                    )
                    await cur.execute(
                        "UPDATE _backup_schedule SET next_run_at=%s WHERE id=1",
                        (next_run,),
                    )
                    return None
                if row[3] > now:
                    return None

                await cur.execute(
                    "SELECT id FROM _backup_jobs "
                    "WHERE status IN ('pending', 'running') LIMIT 1"
                )
                if await cur.fetchone():
                    return None

                await cur.execute(
                    """
                    INSERT INTO _backup_jobs (trigger_source, status)
                    VALUES ('scheduled', 'pending')
                    """
                )
                task_id = cur.lastrowid
                next_run = calculate_next_run_utc(
                    now + timedelta(seconds=1),
                    timezone_name,
                    row[1],
                    row[2],
                )
                await cur.execute(
                    """
                    UPDATE _backup_schedule
                    SET last_triggered_at=UTC_TIMESTAMP(), next_run_at=%s
                    WHERE id=1
                    """,
                    (next_run,),
                )
            finally:
                await _release_lock(cur)
    finally:
        pool.release(conn)
    return task_id


def launch_backup_task(task_id: int) -> None:
    task = asyncio.create_task(run_backup_task(task_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def stop_backup_tasks() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _create_backup_file(task_id: int) -> tuple[str, int, str]:
    backup_dir = Path(settings.BACKUP_DIR).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        backup_dir.chmod(0o700)
    except OSError:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"binhu-db-{stamp}-job{task_id}.sql.gz"
    raw_path = backup_dir / f".{filename}.sql.partial"
    gzip_path = backup_dir / f".{filename}.partial"
    final_path = backup_dir / filename
    manifest_path = backup_dir / f"{filename}.manifest.json"
    manifest_partial = backup_dir / f".{filename}.manifest.partial"
    client = shutil.which("mysqldump") or shutil.which("mariadb-dump")
    if not client:
        raise RuntimeError("容器内没有可用的 mysqldump 客户端")

    command = [
        client,
        f"--host={settings.MYSQL_HOST}",
        f"--port={settings.MYSQL_PORT}",
        f"--user={settings.MYSQL_USER}",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--events",
        "--no-tablespaces",
        "--default-character-set=utf8mb4",
        "--databases",
        *BACKUP_DATABASES,
    ]
    environment = os.environ.copy()
    environment["MYSQL_PWD"] = settings.MYSQL_PASSWORD

    try:
        with raw_path.open("wb") as output:
            completed = subprocess.run(
                command,
                stdout=output,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
                timeout=1800,
            )
        if completed.returncode != 0:
            error = redact_text(
                completed.stderr.decode("utf-8", errors="replace")
            )
            raise RuntimeError(error[-1000:] or "mysqldump 执行失败")
        if raw_path.stat().st_size == 0:
            raise RuntimeError("mysqldump 生成了空文件")

        with raw_path.open("rb") as source, gzip.open(gzip_path, "wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)

        preview = bytearray()
        database_markers = {database.encode("utf-8"): False for database in BACKUP_DATABASES}
        scan_tail = b""
        with gzip.open(gzip_path, "rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                if len(preview) < 1024 * 1024:
                    preview.extend(chunk[: 1024 * 1024 - len(preview)])
                scan = scan_tail + chunk
                for marker in database_markers:
                    if marker in scan:
                        database_markers[marker] = True
                scan_tail = scan[-256:]
        missing = [
            marker for marker, found in database_markers.items() if not found
        ]
        if b"CREATE DATABASE" not in preview or missing:
            names = ",".join(item.decode("utf-8", errors="replace") for item in missing)
            raise RuntimeError(f"八库备份内容完整性检查未通过{': ' + names if names else ''}")

        digest = hashlib.sha256()
        with gzip_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)

        section_manifest = _database_section_manifest(raw_path)
        attachment_manifest = _create_attachment_archive(backup_dir, stamp, task_id)
        os.replace(gzip_path, final_path)
        try:
            final_path.chmod(0o600)
        except OSError:
            pass
        manifest = {
            "schema": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_backup": {
                "filename": filename,
                "size_bytes": final_path.stat().st_size,
                "sha256": digest.hexdigest(),
            },
            "databases": section_manifest,
            "attachments": attachment_manifest,
        }
        manifest_partial.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            manifest_partial.chmod(0o600)
        except OSError:
            pass
        os.replace(manifest_partial, manifest_path)
        return filename, final_path.stat().st_size, digest.hexdigest()
    finally:
        raw_path.unlink(missing_ok=True)
        gzip_path.unlink(missing_ok=True)
        manifest_partial.unlink(missing_ok=True)


def _database_section_manifest(raw_path: Path) -> list[dict]:
    marker = re.compile(rb"(?:Current Database:|USE)\s+`([^`]+)`")
    sections = {
        database: {"database": database, "size_bytes": 0, "sha256": hashlib.sha256()}
        for database in BACKUP_DATABASES
    }
    current: str | None = None
    with raw_path.open("rb") as source:
        for line in source:
            match = marker.search(line)
            if match:
                candidate = match.group(1).decode("utf-8", errors="replace")
                current = candidate if candidate in sections else None
            if current:
                sections[current]["size_bytes"] += len(line)
                sections[current]["sha256"].update(line)
    return [
        {
            "database": database,
            "size_bytes": int(sections[database]["size_bytes"]),
            "sha256": sections[database]["sha256"].hexdigest(),
            "included": sections[database]["size_bytes"] > 0,
        }
        for database in BACKUP_DATABASES
    ]


def _create_attachment_archive(backup_dir: Path, stamp: str, task_id: int) -> dict:
    source_root = Path(settings.WORKFLOW_ATTACHMENT_DIR).resolve()
    if not source_root.is_dir():
        return {"status": "not_present", "file_count": 0}
    filename = f"binhu-attachments-{stamp}-job{task_id}.tar.gz"
    partial = backup_dir / f".{filename}.partial"
    final = backup_dir / filename
    file_entries = []
    try:
        with tarfile.open(partial, "w:gz") as archive:
            for path in sorted(source_root.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve()
                if source_root not in resolved.parents:
                    continue
                relative = resolved.relative_to(source_root)
                digest = hashlib.sha256()
                with resolved.open("rb") as attachment:
                    while chunk := attachment.read(1024 * 1024):
                        digest.update(chunk)
                archive.add(resolved, arcname=relative.as_posix(), recursive=False)
                file_entries.append({"path": relative.as_posix(), "size_bytes": resolved.stat().st_size, "sha256": digest.hexdigest()})
        digest = hashlib.sha256()
        with partial.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        os.replace(partial, final)
        try:
            final.chmod(0o600)
        except OSError:
            pass
        return {
            "status": "success", "filename": filename, "size_bytes": final.stat().st_size,
            "sha256": digest.hexdigest(), "file_count": len(file_entries), "files": file_entries,
        }
    finally:
        partial.unlink(missing_ok=True)


async def run_backup_task(task_id: int) -> None:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE _backup_jobs
                SET status='running', started_at=UTC_TIMESTAMP(),
                    error_message=NULL
                WHERE id=%s
                """,
                (task_id,),
            )
    finally:
        pool.release(conn)

    try:
        filename, size_bytes, checksum = await asyncio.to_thread(
            _create_backup_file,
            task_id,
        )
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE _backup_jobs
                    SET status='success', filename=%s, size_bytes=%s,
                        sha256=%s, finished_at=UTC_TIMESTAMP()
                    WHERE id=%s
                    """,
                    (filename, size_bytes, checksum, task_id),
                )
        finally:
            pool.release(conn)
        await cleanup_expired_backups()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = redact_text(str(exc))[:1000]
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE _backup_jobs
                    SET status='failed', error_message=%s,
                        finished_at=UTC_TIMESTAMP()
                    WHERE id=%s
                    """,
                    (message, task_id),
                )
        finally:
            pool.release(conn)
        try:
            await create_backup_failure_notifications(task_id, message)
        except Exception as notify_error:
            print(
                f"[BACKUP] 失败通知写入失败: task={task_id} "
                f"error={redact_text(str(notify_error))}"
            )


def _safe_platform_path(filename: str) -> Path | None:
    if not PLATFORM_FILENAME.fullmatch(filename):
        return None
    root = Path(settings.BACKUP_DIR).resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root:
        return None
    return candidate


def _related_backup_paths(filename: str) -> list[Path]:
    database_path = _safe_platform_path(filename)
    if database_path is None:
        return []
    root = database_path.parent
    attachment_name = filename.replace("binhu-db-", "binhu-attachments-", 1).replace(".sql.gz", ".tar.gz")
    candidates = [database_path, root / f"{filename}.manifest.json", root / attachment_name]
    return [candidate for candidate in candidates if candidate.resolve().parent == root]


async def cleanup_expired_backups() -> int:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    expired: list[tuple[int, str]] = []
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, filename
                FROM _backup_jobs
                WHERE status='success' AND filename IS NOT NULL
                ORDER BY finished_at DESC, id DESC
                """
            )
            rows = list(await cur.fetchall())
            protected_id = next(
                (
                    job_id
                    for job_id, filename in rows
                    if (
                        (path := _safe_platform_path(filename)) is not None
                        and path.is_file()
                    )
                ),
                None,
            )
            await cur.execute(
                """
                SELECT id, filename
                FROM _backup_jobs
                WHERE status='success' AND filename IS NOT NULL
                  AND finished_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
                """,
            )
            expired = list(await cur.fetchall())

        removed_ids = []
        for job_id, filename in expired:
            if job_id == protected_id:
                continue
            path = _safe_platform_path(filename)
            if path is None:
                continue
            for related in _related_backup_paths(filename):
                related.unlink(missing_ok=True)
            removed_ids.append(job_id)

        if removed_ids:
            async with conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(removed_ids))
                await cur.execute(
                    f"UPDATE _backup_jobs SET status='expired', filename=NULL "
                    f"WHERE id IN ({placeholders})",
                    tuple(removed_ids),
                )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM _admin_audit_log
                WHERE created_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 180 DAY)
                """
            )
        return len(removed_ids)
    finally:
        pool.release(conn)


async def recover_interrupted_backups() -> int:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    task_ids: list[int] = []
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM _backup_jobs "
                "WHERE status IN ('pending', 'running')"
            )
            task_ids = [row[0] for row in await cur.fetchall()]
            if task_ids:
                await cur.execute(
                    """
                    UPDATE _backup_jobs
                    SET status='failed',
                        error_message='服务重启，备份任务中断',
                        finished_at=UTC_TIMESTAMP()
                    WHERE status IN ('pending', 'running')
                    """
                )
    finally:
        pool.release(conn)

    backup_dir = Path(settings.BACKUP_DIR)
    if backup_dir.exists():
        for pattern in (".binhu-db-*.partial", ".binhu-db-*.manifest.partial", ".binhu-attachments-*.partial"):
            for partial in backup_dir.glob(pattern):
                partial.unlink(missing_ok=True)
    for task_id in task_ids:
        try:
            await create_backup_failure_notifications(
                task_id,
                "服务重启，备份任务中断",
            )
        except Exception:
            pass
    return len(task_ids)


async def list_backup_jobs(limit: int = 100) -> dict:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT b.id, b.trigger_source, b.status, b.filename,
                       b.size_bytes, b.sha256, b.error_message,
                       b.started_at, b.finished_at, b.created_at,
                       u.username
                FROM _backup_jobs b
                LEFT JOIN _users u ON u.id=b.requested_by
                ORDER BY b.id DESC
                LIMIT %s
                """,
                (min(max(limit, 1), 100),),
            )
            rows = await cur.fetchall()

        jobs = [
            {
                "id": row[0],
                "trigger_source": row[1],
                "status": row[2],
                "filename": row[3],
                "size_bytes": row[4],
                "sha256": row[5],
                "error_message": row[6],
                "started_at": _iso_utc(row[7]),
                "finished_at": _iso_utc(row[8]),
                "created_at": _iso_utc(row[9]),
                "requested_by": row[10],
            }
            for row in rows
        ]
    finally:
        pool.release(conn)

    known = {job["filename"] for job in jobs if job["filename"]}
    legacy = []
    root = Path(settings.BACKUP_DIR)
    if root.exists():
        for path in sorted(
            (
                item
                for item in root.iterdir()
                if item.is_file()
                and (
                    item.name.endswith(".sql")
                    or item.name.endswith(".sql.gz")
                )
                and item.name not in known
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:100]:
            stat = path.stat()
            legacy.append(
                {
                    "filename": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                }
            )
    return {"data": jobs, "legacy_files": legacy}


async def resolve_backup_file(job_id: int) -> tuple[Path, str] | None:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT filename FROM _backup_jobs
                WHERE id=%s AND status='success' AND filename IS NOT NULL
                """,
                (job_id,),
            )
            row = await cur.fetchone()
    finally:
        pool.release(conn)
    if not row:
        return None
    path = _safe_platform_path(row[0])
    if path is None or not path.is_file():
        return None
    return path, row[0]
