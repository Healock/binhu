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
from collections import defaultdict, deque
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
DEFAULT_WORKER_CONCURRENCY = 3
MAX_WORKER_CONCURRENCY = 3
DEFAULT_CLAIM_LIMIT = 100
MAX_CLAIM_LIMIT = 100
DEFAULT_MICRO_BATCH_SIZE = 25
MAX_MICRO_BATCH_SIZE = 50


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
            available_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
            INDEX idx_projection_job_available (
                status, available_at, created_at, id
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
    has_available_at = await _queue_has_available_at(cur)
    available_column = ",available_at" if has_available_at else ""
    available_value = ",UTC_TIMESTAMP()" if has_available_at else ""
    available_reset = (
        ",available_at=IF(status IN ('failed','retry'), UTC_TIMESTAMP(), available_at)"
        if has_available_at else ""
    )
    await cur.executemany(
        f"""
        INSERT INTO _online_projection_jobs (
            parser_type,row_key,source_id,source_revision,operation_id,status{available_column}
        ) VALUES (%s,%s,%s,%s,%s,'pending'{available_value})
        ON DUPLICATE KEY UPDATE
            source_id=VALUES(source_id),
            operation_id=VALUES(operation_id),
            status=IF(status IN ('failed','retry'), 'pending', status),
            next_attempt_at=IF(status IN ('failed','retry'), NULL, next_attempt_at),
            error_code=IF(status IN ('failed','retry'), '', error_code)
            {available_reset}
        """,
        [(parser_type, key, source_id, revision, operation_id) for key in keys],
    )
    projection_job_telemetry.observe_enqueued(len(keys))
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
    claimed_count: int = 0
    coalesced_count: int = 0
    processed_key_count: int = 0
    revision_skipped_count: int = 0
    split_retry_count: int = 0
    lock_split_count: int = 0
    retry_count: int = 0
    durations_ms: list[float] = field(default_factory=list)
    batch_sizes: list[int] = field(default_factory=list)
    enqueued_at: deque[float] = field(default_factory=deque)
    processed_at: deque[float] = field(default_factory=deque)

    def observe_duration(self, value: float) -> None:
        self.durations_ms.append(round(value, 1))
        if len(self.durations_ms) > 200:
            del self.durations_ms[:-200]

    def observe_enqueued(self, count: int) -> None:
        now = time.monotonic()
        self.enqueued_at.extend([now] * max(0, int(count)))
        self._trim(now)

    def observe_completed(self, count: int) -> None:
        now = time.monotonic()
        amount = max(0, int(count))
        self.processed_at.extend([now] * amount)
        self._trim(now)

    def observe_batch(self, size: int, duration_ms: float) -> None:
        self.batch_sizes.append(max(0, int(size)))
        if len(self.batch_sizes) > 200:
            del self.batch_sizes[:-200]
        self.observe_duration(duration_ms)

    def _trim(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.monotonic()) - 60
        while self.enqueued_at and self.enqueued_at[0] < cutoff:
            self.enqueued_at.popleft()
        while self.processed_at and self.processed_at[0] < cutoff:
            self.processed_at.popleft()

    def rates(self) -> tuple[int, int]:
        self._trim()
        return len(self.enqueued_at), len(self.processed_at)


projection_job_telemetry = ProjectionJobTelemetry()
_available_at_supported: bool | None = None
_available_at_checked_at = 0.0
AVAILABLE_AT_NEGATIVE_CACHE_SECONDS = 5.0


def _online_pool():
    # Imported lazily because database bootstrap imports the schema helper from
    # this module before DatabaseManager has finished being defined.
    from database import db_manager

    return db_manager.get_pool("online_data")


async def _claim_jobs(limit: int) -> list[dict[str, Any]]:
    pool = _online_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            available_at = await _queue_has_available_at(cur)
            await cur.execute("SET SESSION innodb_lock_wait_timeout=%s", (LOCK_WAIT_SECONDS,))
            await conn.begin()
            try:
                due_sql = (
                    "available_at<=UTC_TIMESTAMP()"
                    if available_at
                    else "(next_attempt_at IS NULL OR next_attempt_at<=UTC_TIMESTAMP())"
                )
                await cur.execute(
                    f"""
                    SELECT id,parser_type,row_key,source_id,source_revision,operation_id,attempt_count
                    FROM _online_projection_jobs
                    WHERE status IN ('pending','retry')
                      AND {due_sql}
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
    projection_job_telemetry.claimed_count += len(rows)
    return [
        {
            "id": int(row[0]), "parser_type": str(row[1]), "row_key": str(row[2]),
            "source_id": int(row[3]) if row[3] is not None else None,
            "revision": int(row[4]), "operation_id": str(row[5]),
            "attempt_count": int(row[6]) + 1,
        }
        for row in rows
    ]


async def _queue_has_available_at(cur) -> bool:
    global _available_at_supported, _available_at_checked_at
    now = time.monotonic()
    # A shadow/production migration can add the column while the worker is
    # already running.  Cache a positive result permanently, but only cache a
    # negative result briefly so the worker adopts the indexed path without a
    # process restart.
    if _available_at_supported is True:
        return _available_at_supported
    if (
        _available_at_supported is False
        and now - _available_at_checked_at < AVAILABLE_AT_NEGATIVE_CACHE_SECONDS
    ):
        return False
    await cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name='_online_projection_jobs' "
        "AND column_name='available_at'"
    )
    row = await cur.fetchone()
    _available_at_supported = bool(row and int(row[0] or 0))
    _available_at_checked_at = now
    return _available_at_supported


async def _finish_job(job_id: int, status: str, error_code: str = "") -> None:
    await _finish_jobs([job_id], status, error_code)


async def _finish_jobs(job_ids: list[int], status: str, error_code: str = "") -> None:
    ids = sorted({int(value) for value in job_ids})
    if not ids:
        return
    pool = _online_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            available_at = await _queue_has_available_at(cur)
            placeholders = ",".join(["%s"] * len(ids))
            if status == "retry":
                available_sql = (
                    ",available_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL 2 SECOND)"
                    if available_at else ""
                )
                await cur.execute(
                    f"UPDATE _online_projection_jobs SET status='retry',error_code=%s,"
                    f"next_attempt_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL 2 SECOND){available_sql} "
                    f"WHERE id IN ({placeholders})",
                    (error_code, *ids),
                )
                projection_job_telemetry.retry_count += len(ids)
            else:
                await cur.execute(
                    "UPDATE _online_projection_jobs SET status=%s,error_code=%s,"
                    f"finished_at=UTC_TIMESTAMP(),next_attempt_at=NULL WHERE id IN ({placeholders})",
                    (status, error_code, *ids),
                )
            await conn.commit()


async def _process_job(job: dict[str, Any]) -> None:
    await _process_batch([job])


async def _current_revisions(cur, parser_type: str, row_keys: list[str], *, lock: bool) -> dict[str, int]:
    keys = list(dict.fromkeys(str(value) for value in row_keys if str(value)))
    if not keys:
        return {}
    placeholders = ",".join(["%s"] * len(keys))
    lock_sql = " FOR UPDATE" if lock else ""
    await cur.execute(
        "SELECT source.row_key,source.revision FROM _online_source_rows AS source "
        f"WHERE source.parser_type=%s AND source.row_key IN ({placeholders}) "
        "AND source.archived_at IS NULL"
        + active_source_sql_filter(parser_type)
        + lock_sql,
        (parser_type, *keys),
    )
    revisions: dict[str, int] = {}
    for row_key, revision in await cur.fetchall():
        key = str(row_key)
        revisions[key] = max(revisions.get(key, 0), int(revision or 0))
    return revisions


async def _mark_jobs_in_transaction(cur, jobs: list[dict[str, Any]], status: str, error_code: str = "") -> None:
    ids = [int(job["id"]) for job in jobs]
    if not ids:
        return
    placeholders = ",".join(["%s"] * len(ids))
    await cur.execute(
        f"UPDATE _online_projection_jobs SET status=%s,error_code=%s,"
        f"finished_at=UTC_TIMESTAMP(),next_attempt_at=NULL WHERE id IN ({placeholders})",
        (status, error_code, *ids),
    )


async def _process_batch(jobs: list[dict[str, Any]]) -> None:
    if not jobs:
        return
    started = time.perf_counter()
    projection_job_telemetry.processing += len(jobs)
    try:
        pool = _online_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET SESSION innodb_lock_wait_timeout=%s", (LOCK_WAIT_SECONDS,))
                await cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                await conn.begin()
                try:
                    parser_type = str(jobs[0]["parser_type"])
                    revisions = await _current_revisions(
                        cur, parser_type, [str(job["row_key"]) for job in jobs], lock=False,
                    )
                    stale = [
                        job for job in jobs
                        if revisions.get(str(job["row_key"])) is not None
                        and revisions[str(job["row_key"])] > int(job["revision"])
                    ]
                    active = [job for job in jobs if job not in stale]
                    if stale:
                        await _mark_jobs_in_transaction(cur, stale, "skipped", "stale_revision")
                    if active:
                        keys = [str(job["row_key"]) for job in active]
                        await rebuild_projection_keys(
                            cur, parser_type, keys, reconcile_graph=False,
                        )
                        await reconcile_projection_task_graph_rows(cur, parser_type, keys)
                        # Lock only for the final revision fence.  This prevents
                        # a concurrent save from committing between the last
                        # revision check and this derived transaction's commit.
                        final_revisions = await _current_revisions(cur, parser_type, keys, lock=True)
                        changed = [
                            job for job in active
                            if final_revisions.get(str(job["row_key"])) is not None
                            and final_revisions[str(job["row_key"])] != int(job["revision"])
                        ]
                        if changed:
                            raise RuntimeError("projection_revision_advanced")
                        await _mark_jobs_in_transaction(cur, active, "succeeded")
                    await conn.commit()
                    projection_job_telemetry.skipped += len(stale)
                    projection_job_telemetry.succeeded += len(active)
                    projection_job_telemetry.revision_skipped_count += len(stale)
                    projection_job_telemetry.processed_key_count += len(active)
                    projection_job_telemetry.observe_completed(len(jobs))
                except BaseException:
                    await conn.rollback()
                    raise
    except asyncio.CancelledError:
        await _finish_jobs([int(job["id"]) for job in jobs], "retry", "worker_cancelled")
        raise
    except Exception as exc:
        code = _safe_error_code(exc)
        if str(exc) == "projection_revision_advanced":
            code = "stale_revision_race"
        projection_job_telemetry.recent_error_code = code
        if len(jobs) > 1:
            projection_job_telemetry.split_retry_count += 1
            if code in {"projection_deadlock", "projection_lock_timeout"}:
                projection_job_telemetry.lock_split_count += 1
            midpoint = len(jobs) // 2
            await _process_batch(jobs[:midpoint])
            await _process_batch(jobs[midpoint:])
        else:
            job = jobs[0]
            if int(job["attempt_count"]) < MAX_ATTEMPTS:
                await _finish_job(int(job["id"]), "retry", code)
            else:
                await _finish_job(int(job["id"]), "failed", code)
                projection_job_telemetry.failed += 1
    finally:
        projection_job_telemetry.processing = max(0, projection_job_telemetry.processing - len(jobs))
        projection_job_telemetry.observe_batch(
            len(jobs), (time.perf_counter() - started) * 1000,
        )


async def process_projection_jobs_once(limit: int = 10) -> int:
    jobs = await _claim_jobs(max(1, min(int(limit), MAX_CLAIM_LIMIT)))
    if not jobs:
        return 0
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    coalesced: list[dict[str, Any]] = []
    for job in jobs:
        key = (str(job["parser_type"]), str(job["row_key"]))
        current = newest.get(key)
        if current is None or int(job["revision"]) > int(current["revision"]):
            if current is not None:
                coalesced.append(current)
            newest[key] = job
        else:
            coalesced.append(job)
    if coalesced:
        await _finish_jobs(
            [int(job["id"]) for job in coalesced], "skipped", "coalesced_revision",
        )
        projection_job_telemetry.coalesced_count += len(coalesced)
        projection_job_telemetry.skipped += len(coalesced)
        projection_job_telemetry.observe_completed(len(coalesced))

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in newest.values():
        grouped[str(job["parser_type"])].append(job)
    batch_size = _micro_batch_size()
    batches = [
        parser_jobs[index:index + batch_size]
        for parser_jobs in grouped.values()
        for index in range(0, len(parser_jobs), batch_size)
    ]
    semaphore = asyncio.Semaphore(_worker_concurrency())

    async def process_bounded(batch: list[dict[str, Any]]) -> None:
        async with semaphore:
            await _process_batch(batch)

    await asyncio.gather(*(process_bounded(batch) for batch in batches))
    return len(jobs)


def _worker_concurrency() -> int:
    """Return a safe, bounded worker concurrency from deployment config."""
    raw = os.environ.get("ONLINE_PROJECTION_WORKER_CONCURRENCY", "")
    try:
        value = int(raw) if raw.strip() else DEFAULT_WORKER_CONCURRENCY
    except ValueError:
        value = DEFAULT_WORKER_CONCURRENCY
    return max(1, min(value, MAX_WORKER_CONCURRENCY))


def _micro_batch_size() -> int:
    raw = os.environ.get("ONLINE_PROJECTION_MICRO_BATCH_SIZE", "")
    try:
        value = int(raw) if raw.strip() else DEFAULT_MICRO_BATCH_SIZE
    except ValueError:
        value = DEFAULT_MICRO_BATCH_SIZE
    return max(1, min(value, MAX_MICRO_BATCH_SIZE))


def _claim_limit() -> int:
    raw = os.environ.get("ONLINE_PROJECTION_CLAIM_LIMIT", "")
    try:
        value = int(raw) if raw.strip() else DEFAULT_CLAIM_LIMIT
    except ValueError:
        value = DEFAULT_CLAIM_LIMIT
    return max(1, min(value, MAX_CLAIM_LIMIT))


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return round(ordered[index], 1)


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
    batch_sizes = list(projection_job_telemetry.batch_sizes)
    enqueue_rate, process_rate = projection_job_telemetry.rates()
    return {
        "queued_count": int((row or [0])[0] or 0),
        "running_count": int((row or [0, 0])[1] or 0),
        "succeeded_count": int((row or [0, 0, 0])[2] or 0),
        "skipped_count": int((row or [0, 0, 0, 0])[3] or 0),
        "failed_count": int((row or [0, 0, 0, 0, 0])[4] or 0),
        "oldest_wait_seconds": max(0, int((row or [0] * 6)[5] or 0)),
        "average_duration_ms": round(sum(durations) / len(durations), 1) if durations else 0,
        "max_duration_ms": round(max(durations), 1) if durations else 0,
        "batch_p50_ms": _percentile(durations, 0.50),
        "batch_p95_ms": _percentile(durations, 0.95),
        "micro_batch_size": _micro_batch_size(),
        "average_batch_size": round(sum(batch_sizes) / len(batch_sizes), 1) if batch_sizes else 0,
        "enqueue_rate_1m": enqueue_rate,
        "process_rate_1m": process_rate,
        "claimed_count": projection_job_telemetry.claimed_count,
        "coalesced_count": projection_job_telemetry.coalesced_count,
        "processed_key_count": projection_job_telemetry.processed_key_count,
        "revision_skipped_count": projection_job_telemetry.revision_skipped_count,
        "split_retry_count": projection_job_telemetry.split_retry_count,
        "lock_split_count": projection_job_telemetry.lock_split_count,
        "retry_count": projection_job_telemetry.retry_count,
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
            processed = await process_projection_jobs_once(limit=_claim_limit())
            await asyncio.sleep(0 if processed else POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            projection_job_telemetry.recent_error_code = "projection_worker_loop_failed"
            await asyncio.sleep(1)


def new_save_operation_id() -> str:
    return str(uuid.uuid4())
