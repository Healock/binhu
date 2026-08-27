"""Privacy-safe accounting for outbound Tencent Docs API attempts."""

import asyncio
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlsplit

from database import db_manager


QUOTA_EXHAUSTED_CODE = "400011"
_PENDING_EVENTS: deque[dict] = deque()
_MAX_PENDING_EVENTS = 50000
_WORKER_TASK: asyncio.Task | None = None


def classify_txdocs_endpoint(url: str) -> str:
    """Collapse request URLs so file, sheet and range identifiers are not stored."""
    parsed = urlsplit(str(url or ""))
    path = parsed.path
    query = parsed.query
    if path.endswith("/batchUpdate"):
        return "batch_update"
    if path.endswith(":clear"):
        return "range_clear"
    if path.startswith("/files/") and "concise=1" in query:
        return "file_info"
    if path.startswith("/files/"):
        return "range_read"
    return "other"


async def record_txdocs_request(
    *,
    request_source: str,
    method: str,
    endpoint: str,
    success: bool,
    retry: bool,
    http_status: int | None,
    error_code: str | int | None,
) -> None:
    """Queue one actual HTTP attempt without blocking the Tencent request."""
    global _WORKER_TASK
    event = {
        "bucket_hour": datetime.now(timezone.utc).replace(
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=None,
        ),
        "request_source": str(request_source or "unknown")[:40],
        "endpoint": str(endpoint or "other")[:40],
        "method": str(method or "GET").upper()[:10],
        "success": int(success),
        "failure": int(not success),
        "retry": int(retry),
        "quota_exhausted": int(
            str(error_code or "") == QUOTA_EXHAUSTED_CODE
        ),
        "http_status": http_status,
        "error_code": str(error_code or "")[:40],
    }
    while len(_PENDING_EVENTS) >= _MAX_PENDING_EVENTS:
        await asyncio.sleep(0.01)
    _PENDING_EVENTS.append(event)
    if _WORKER_TASK is None or _WORKER_TASK.done():
        _WORKER_TASK = asyncio.create_task(_run_usage_worker())


async def _run_usage_worker() -> None:
    while _PENDING_EVENTS:
        batch = [
            _PENDING_EVENTS.popleft()
            for _ in range(min(1000, len(_PENDING_EVENTS)))
        ]
        await _persist_usage_batch(batch)
        await asyncio.sleep(0)


async def _persist_usage_batch(events: list[dict]) -> None:
    grouped: dict[tuple, dict] = {}
    for event in events:
        key = (
            event["bucket_hour"],
            event["request_source"],
            event["endpoint"],
            event["method"],
        )
        bucket = grouped.setdefault(
            key,
            {
                "attempts": 0,
                "success": 0,
                "failure": 0,
                "retries": 0,
                "quota_exhausted": 0,
                "http_status": None,
                "error_code": "",
            },
        )
        bucket["attempts"] += 1
        bucket["success"] += event["success"]
        bucket["failure"] += event["failure"]
        bucket["retries"] += event["retry"]
        bucket["quota_exhausted"] += event["quota_exhausted"]
        bucket["http_status"] = event["http_status"]
        bucket["error_code"] = event["error_code"]

    pool = None
    conn = None
    try:
        pool = db_manager.get_pool("online_data")
        conn = await pool.acquire()
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO _txdocs_api_usage_hourly (
                    bucket_hour, request_source, endpoint, method,
                    attempt_count, success_count, failure_count,
                    retry_count, quota_exhausted_count,
                    last_http_status, last_error_code
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    attempt_count=attempt_count+VALUES(attempt_count),
                    success_count=success_count+VALUES(success_count),
                    failure_count=failure_count+VALUES(failure_count),
                    retry_count=retry_count+VALUES(retry_count),
                    quota_exhausted_count=(
                        quota_exhausted_count+VALUES(quota_exhausted_count)
                    ),
                    last_http_status=VALUES(last_http_status),
                    last_error_code=VALUES(last_error_code)
                """,
                [
                    (
                        *key,
                        values["attempts"],
                        values["success"],
                        values["failure"],
                        values["retries"],
                        values["quota_exhausted"],
                        values["http_status"],
                        values["error_code"],
                    )
                    for key, values in grouped.items()
                ],
            )
    except Exception:
        # Metrics must never make a Tencent request fail or trigger a retry.
        return
    finally:
        if pool is not None and conn is not None:
            try:
                pool.release(conn)
            except Exception:
                pass


async def stop_txdocs_usage_tasks() -> None:
    global _WORKER_TASK
    if _WORKER_TASK is not None and not _WORKER_TASK.done():
        try:
            await asyncio.wait_for(_WORKER_TASK, timeout=5)
        except asyncio.TimeoutError:
            _WORKER_TASK.cancel()
            try:
                await _WORKER_TASK
            except asyncio.CancelledError:
                pass
    _WORKER_TASK = None
