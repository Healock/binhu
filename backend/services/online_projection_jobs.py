"""Durable post-commit projection work for local online-task saves.

The request transaction only updates the authoritative task rows and the
small projection fields needed by the current screen.  Address matching,
watch-person snapshots and WorkflowData task-graph reconciliation run here
after the save has committed.  Queue rows contain identifiers and revisions
only; no task body or sensitive person data is copied into the queue.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from services.local_source import local_row_hash
from services.online_source import (
    active_source_sql_filter,
    assignment_projection_fields,
    rebuild_projection_keys,
    stable_json,
)
from services.parsers import get_parser
from services.task_graph import reconcile_projection_task_graph_rows
from services.task_workflow import task_state
from services.watch_matching import projection_identity


POLL_SECONDS = 0.35
LOCK_WAIT_SECONDS = 3
MAX_ATTEMPTS = 5
DEFAULT_WORKER_CONCURRENCY = 4
MAX_WORKER_CONCURRENCY = 8


async def ensure_online_projection_job_schema(cur) -> None:
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _online_projection_jobs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            source_id BIGINT DEFAULT NULL,
            source_revision BIGINT UNSIGNED NOT NULL,
            operation_id CHAR(36) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
            error_code VARCHAR(80) NOT NULL DEFAULT '',
            next_attempt_at DATETIME DEFAULT NULL,
            started_at DATETIME DEFAULT NULL,
            finished_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_projection_job_revision (
                parser_type, row_key, source_revision
            ),
            INDEX idx_projection_job_due (
                status, next_attempt_at, created_at
            ),
            INDEX idx_projection_job_source (source_id, source_revision)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )


async def enqueue_projection_jobs(
    cur,
    *,
    parser_type: str,
    row_keys: list[str],
    source_id: int,
    revision: int,
    operation_id: str,
) -> int:
    keys = list(dict.fromkeys(str(value).strip() for value in row_keys if str(value).strip()))
    if not keys:
        return 0
    await cur.executemany(
        """
        INSERT INTO _online_projection_jobs (
            parser_type,row_key,source_id,source_revision,operation_id,status
        ) VALUES (%s,%s,%s,%s,%s,'pending')
        ON DUPLICATE KEY UPDATE
            source_id=VALUES(source_id),
            operation_id=VALUES(operation_id),
            status=IF(status IN ('failed','retry'), 'pending', status),
            next_attempt_at=IF(status IN ('failed','retry'), NULL, next_attempt_at),
            error_code=IF(status IN ('failed','retry'), '', error_code)
        """,
        [(parser_type, key, source_id, revision, operation_id) for key in keys],
    )
    return len(keys)


async def update_lightweight_projection(
    cur,
    *,
    parser_type: str,
    row_key_before: str,
    row_key_after: str,
    values: dict[str, str],
    revision: int,
) -> None:
    """Keep the current list/detail usable without doing expensive matching."""
    parser = get_parser(parser_type)
    community = parser.community_value(values)
    projected_state = task_state(parser_type, values)
    source_label, address_display, address_sort_key, queue_ready = assignment_projection_fields(
        parser_type,
        values,
        community=community,
        source_count=1,
        conflict=False,
        task_state_value=projected_state,
    )
    values_json = stable_json(values)
    search_text = "\n".join(str(values.get(column, "") or "") for column in parser.COLUMNS)
    inspector = str(values.get("核查人", "") or "").strip()
    identity_hmac = projection_identity(parser_type, values, parser.COLUMNS)

    if row_key_before != row_key_after:
        await cur.execute(
            "UPDATE _online_task_address_matches SET row_key=%s "
            "WHERE parser_type=%s AND row_key=%s",
            (row_key_after, parser_type, row_key_before),
        )
        await cur.execute(
            "UPDATE _online_source_projection SET row_key=%s "
            "WHERE parser_type=%s AND row_key=%s",
            (row_key_after, parser_type, row_key_before),
        )

    await cur.execute(
        """
        UPDATE _online_source_projection
        SET values_json=%s,community=%s,inspector=%s,identity_hmac=%s,
            task_state=%s,search_text=%s,source_revision=%s,
            assignment_source_label=%s,assignment_address_display=%s,
            assignment_address_sort_key=%s,assignment_queue_ready=%s
        WHERE parser_type=%s AND row_key=%s
        """,
        (
            values_json, community, inspector, identity_hmac,
            projected_state, search_text, revision,
            source_label, address_display, address_sort_key, queue_ready,
            parser_type, row_key_after,
        ),
    )
    if cur.rowcount:
        return
    await cur.execute(
        """
        INSERT INTO _online_source_projection (
            parser_type,row_key,values_json,community,inspector,identity_hmac,
            task_state,source_count,conflict,search_text,source_revision,
            assignment_source_label,assignment_address_display,
            assignment_address_sort_key,assignment_queue_ready
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,1,0,%s,%s,%s,%s,%s,%s)
        """,
        (
            parser_type, row_key_after, values_json, community, inspector,
            identity_hmac, projected_state, search_text, revision,
            source_label, address_display, address_sort_key, queue_ready,
        ),
    )


def _mysql_errno(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        args = getattr(current, "args", ())
        if args and isinstance(args[0], int):
            return int(args[0])
        current = current.__cause__ or current.__context__
    return None


def _safe_error_code(exc: BaseException) -> str:
    errno = _mysql_errno(exc)
    if errno == 1213:
        return "projection_deadlock"
    if errno == 1205:
        return "projection_lock_timeout"
    return "projection_failed"


@dataclass
class ProjectionJobTelemetry:
    processing: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    recent_error_code: str = ""
    durations_ms: list[float] = field(default_factory=list)

    def observe_duration(self, value: float) -> None:
        self.durations_ms.append(round(value, 1))
        if len(self.durations_ms) > 200:
            del self.durations_ms[:-200]


projection_job_telemetry = ProjectionJobTelemetry()


def _online_pool():
    # Imported lazily because database bootstrap imports the schema helper from
    # this module before DatabaseManager has finished being defined.
    from database import db_manager

    return db_manager.get_pool("online_data")


async def _claim_jobs(limit: int) -> list[dict[str, Any]]:
    pool = _online_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SET SESSION innodb_lock_wait_timeout=%s", (LOCK_WAIT_SECONDS,))
            await conn.begin()
            try:
                await cur.execute(
                    """
                    SELECT id,parser_type,row_key,source_id,source_revision,operation_id,attempt_count
                    FROM _online_projection_jobs
                    WHERE status IN ('pending','retry')
                      AND (next_attempt_at IS NULL OR next_attempt_at<=UTC_TIMESTAMP())
                    ORDER BY created_at,id
                    LIMIT %s FOR UPDATE SKIP LOCKED
                    """,
                    (limit,),
                )
                rows = await cur.fetchall()
                ids = [int(row[0]) for row in rows]
                if ids:
                    placeholders = ",".join(["%s"] * len(ids))
                    await cur.execute(
                        f"UPDATE _online_projection_jobs SET status='running',started_at=UTC_TIMESTAMP(),"
                        f"attempt_count=attempt_count+1,error_code='' WHERE id IN ({placeholders})",
                        ids,
                    )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    return [
        {
            "id": int(row[0]), "parser_type": str(row[1]), "row_key": str(row[2]),
            "source_id": int(row[3]) if row[3] is not None else None,
            "revision": int(row[4]), "operation_id": str(row[5]),
            "attempt_count": int(row[6]) + 1,
        }
        for row in rows
    ]


async def _finish_job(job_id: int, status: str, error_code: str = "") -> None:
    pool = _online_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            if status == "retry":
                await cur.execute(
                    "UPDATE _online_projection_jobs SET status='retry',error_code=%s,"
                    "next_attempt_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL 2 SECOND) WHERE id=%s",
                    (error_code, job_id),
                )
            else:
                await cur.execute(
                    "UPDATE _online_projection_jobs SET status=%s,error_code=%s,"
                    "finished_at=UTC_TIMESTAMP(),next_attempt_at=NULL WHERE id=%s",
                    (status, error_code, job_id),
                )
            await conn.commit()


async def _process_job(job: dict[str, Any]) -> None:
    started = time.perf_counter()
    projection_job_telemetry.processing += 1
    try:
        pool = _online_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET SESSION innodb_lock_wait_timeout=%s", (LOCK_WAIT_SECONDS,))
                await conn.begin()
                try:
                    await cur.execute(
                        "SELECT MAX(revision) FROM _online_source_rows AS source "
                        "WHERE parser_type=%s AND row_key=%s AND archived_at IS NULL"
                        + active_source_sql_filter(str(job["parser_type"])),
                        (job["parser_type"], job["row_key"]),
                    )
                    row = await cur.fetchone()
                    current_revision = int(row[0]) if row and row[0] is not None else None
                    if current_revision is not None and current_revision > int(job["revision"]):
                        # Finish the stale row with the connection already held
                        # by this worker.  Acquiring a second online-data
                        # connection here can deadlock the whole pool when
                        # request traffic temporarily occupies every other
                        # slot.
                        await cur.execute(
                            "UPDATE _online_projection_jobs SET status='skipped',"
                            "error_code='stale_revision',finished_at=UTC_TIMESTAMP(),"
                            "next_attempt_at=NULL WHERE id=%s",
                            (job["id"],),
                        )
                        await conn.commit()
                        projection_job_telemetry.skipped += 1
                        return
                    await rebuild_projection_keys(
                        cur, str(job["parser_type"]), [str(job["row_key"])],
                        reconcile_graph=False,
                    )
                    await reconcile_projection_task_graph_rows(
                        cur, str(job["parser_type"]), [str(job["row_key"])],
                    )
                    await cur.execute(
                        "UPDATE _online_projection_jobs SET status='succeeded',error_code='',"
                        "finished_at=UTC_TIMESTAMP() WHERE id=%s",
                        (job["id"],),
                    )
                    await conn.commit()
                    projection_job_telemetry.succeeded += 1
                except BaseException:
                    await conn.rollback()
                    raise
    except asyncio.CancelledError:
        await _finish_job(job["id"], "retry", "worker_cancelled")
        raise
    except Exception as exc:
        code = _safe_error_code(exc)
        projection_job_telemetry.recent_error_code = code
        if int(job["attempt_count"]) < MAX_ATTEMPTS:
            await _finish_job(job["id"], "retry", code)
        else:
            await _finish_job(job["id"], "failed", code)
            projection_job_telemetry.failed += 1
    finally:
        projection_job_telemetry.processing = max(0, projection_job_telemetry.processing - 1)
        projection_job_telemetry.observe_duration((time.perf_counter() - started) * 1000)


async def process_projection_jobs_once(limit: int = 10) -> int:
    jobs = await _claim_jobs(max(1, min(int(limit), 50)))
    if not jobs:
        return 0
    # Each job owns its own short transaction and connection.  Keep the
    # fan-out bounded so the worker cannot consume the entire online-data
    # pool while request traffic is active (the pool has ten connections per
    # business database in production).
    semaphore = asyncio.Semaphore(_worker_concurrency())

    async def process_bounded(job: dict[str, Any]) -> None:
        async with semaphore:
            await _process_job(job)

    await asyncio.gather(*(process_bounded(job) for job in jobs))
    return len(jobs)


def _worker_concurrency() -> int:
    """Return a safe, bounded worker concurrency from deployment config."""
    raw = os.environ.get("ONLINE_PROJECTION_WORKER_CONCURRENCY", "")
    try:
        value = int(raw) if raw.strip() else DEFAULT_WORKER_CONCURRENCY
    except ValueError:
        value = DEFAULT_WORKER_CONCURRENCY
    return max(1, min(value, MAX_WORKER_CONCURRENCY))


async def projection_queue_snapshot() -> dict[str, Any]:
    pool = _online_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    SUM(status IN ('pending','retry')),
                    SUM(status='running'),
                    SUM(status='succeeded'),
                    SUM(status='skipped'),
                    SUM(status='failed'),
                    TIMESTAMPDIFF(SECOND, MIN(CASE WHEN status IN ('pending','retry') THEN created_at END), UTC_TIMESTAMP())
                FROM _online_projection_jobs
                """
            )
            row = await cur.fetchone()
    durations = list(projection_job_telemetry.durations_ms)
    return {
        "queued_count": int((row or [0])[0] or 0),
        "running_count": int((row or [0, 0])[1] or 0),
        "succeeded_count": int((row or [0, 0, 0])[2] or 0),
        "skipped_count": int((row or [0, 0, 0, 0])[3] or 0),
        "failed_count": int((row or [0, 0, 0, 0, 0])[4] or 0),
        "oldest_wait_seconds": max(0, int((row or [0] * 6)[5] or 0)),
        "average_duration_ms": round(sum(durations) / len(durations), 1) if durations else 0,
        "max_duration_ms": round(max(durations), 1) if durations else 0,
        "recent_error_code": projection_job_telemetry.recent_error_code,
    }


async def run_online_projection_worker() -> None:
    pool = _online_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _online_projection_jobs SET status='retry',error_code='worker_recovered',"
                "next_attempt_at=NULL WHERE status='running'"
            )
        await conn.commit()
    while True:
        try:
            # Claim a bounded batch and process it concurrently.  Each job
            # still has an independent transaction, and _process_job marks a
            # cancelled row retryable before propagating cancellation.
            processed = await process_projection_jobs_once(limit=_worker_concurrency())
            await asyncio.sleep(0 if processed else POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            projection_job_telemetry.recent_error_code = "projection_worker_loop_failed"
            await asyncio.sleep(1)


def new_save_operation_id() -> str:
    return str(uuid.uuid4())
