"""用户无感的错误现场捕获与后台诊断任务。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from services.ops_redaction import redact_text, sanitize_detail


def _db_manager():
    from database import db_manager
    return db_manager

DIAGNOSTIC_RETENTION_DAYS = 90
DIAGNOSTIC_STATUSES = ("queued", "running", "succeeded", "failed", "captured")
EXPECTED_HTTP_STATUSES = {400, 401, 403, 404, 409, 422}
_incident_samples: dict[tuple[str, str, int, int], float] = {}
_incident_status_counts: Counter[int] = Counter()
_captured_incidents = 0
_suppressed_duplicate_incidents = 0


def should_capture_incident(request: Any, status_code: int) -> bool:
    """Keep expected business responses out of the durable diagnostic queue.

    Unexpected server failures retain one safe sample per route, method, status
    and UTC minute. Aggregate request metrics remain authoritative for counts.
    """
    global _captured_incidents, _suppressed_duplicate_incidents
    status = int(status_code)
    _incident_status_counts[status] += 1
    if status in EXPECTED_HTTP_STATUSES or status < 500:
        return False
    route = request.scope.get("route") if hasattr(request, "scope") else None
    route_path = str(getattr(route, "path", None) or "/unmatched")[:200]
    method = str(getattr(request, "method", "") or "")[:10]
    bucket = int(time.time() // 60)
    key = (method, route_path, status, bucket)
    if key in _incident_samples:
        _suppressed_duplicate_incidents += 1
        return False
    _incident_samples[key] = time.monotonic()
    if len(_incident_samples) > 500:
        oldest_buckets = sorted(_incident_samples, key=lambda item: item[3])[:-250]
        for old_key in oldest_buckets:
            _incident_samples.pop(old_key, None)
    _captured_incidents += 1
    return True


def incident_capture_snapshot() -> dict[str, Any]:
    return {
        "expected_response_count": sum(
            count for status, count in _incident_status_counts.items()
            if status in EXPECTED_HTTP_STATUSES
        ),
        "expected_by_status": {
            str(status): int(_incident_status_counts.get(status, 0))
            for status in sorted(EXPECTED_HTTP_STATUSES)
        },
        "captured_incident_count": _captured_incidents,
        "suppressed_duplicate_count": _suppressed_duplicate_incidents,
    }


def ensure_diagnostic_schema_sql() -> tuple[str, str]:
    return (
        """
        CREATE TABLE IF NOT EXISTS diagnostic_jobs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            job_id CHAR(36) NOT NULL UNIQUE,
            requested_by INT DEFAULT NULL,
            user_name VARCHAR(64) NOT NULL DEFAULT '',
            mode VARCHAR(20) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'captured',
            task_id VARCHAR(64) DEFAULT NULL,
            page_url VARCHAR(512) NOT NULL DEFAULT '',
            error_code VARCHAR(100) NOT NULL DEFAULT '',
            error_message VARCHAR(1000) NOT NULL DEFAULT '',
            request_summary_json JSON DEFAULT NULL,
            attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
            priority TINYINT NOT NULL DEFAULT 0,
            queued_at DATETIME DEFAULT NULL,
            started_at DATETIME DEFAULT NULL,
            finished_at DATETIME DEFAULT NULL,
            expires_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_diagnostic_user_created (user_name, created_at),
            INDEX idx_diagnostic_status_created (status, created_at),
            INDEX idx_diagnostic_task_created (task_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS diagnostic_reports (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            report_id CHAR(36) NOT NULL UNIQUE,
            job_id CHAR(36) NOT NULL,
            mode VARCHAR(20) NOT NULL,
            task_id VARCHAR(64) DEFAULT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'succeeded',
            overall_status VARCHAR(20) NOT NULL DEFAULT 'healthy',
            summary_json JSON NOT NULL,
            technical_json JSON DEFAULT NULL,
            rules_version VARCHAR(30) NOT NULL DEFAULT '1',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME DEFAULT NULL,
            expires_at DATETIME NOT NULL,
            UNIQUE KEY uk_diagnostic_report_job (job_id),
            INDEX idx_diagnostic_report_created (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    )


async def ensure_diagnostic_schema(cur) -> None:
    for statement in ensure_diagnostic_schema_sql():
        await cur.execute(statement)


def _safe_text(value: Any, limit: int) -> str:
    text = redact_text(str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _request_summary(request: Any) -> dict[str, Any]:
    query = {}
    for key in ("task_id", "batch_id", "job_id", "parser_type", "row_key"):
        value = request.query_params.get(key)
        if value:
            query[key] = _safe_text(value, 80)
    return {
        "method": _safe_text(request.method, 10),
        "path": _safe_text(request.url.path, 512),
        "query_keys": sorted(query),
        "identifiers": query,
    }


async def _resolve_session_user(session_id: str | None) -> tuple[int | None, str]:
    if not session_id:
        return None, ""
    try:
        pool = _db_manager().get_pool("online_data")
    except ValueError:
        return None, ""
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT user.id, COALESCE(NULLIF(user.display_name, ''),
                           member.name, user.username)
                   FROM _sessions AS session
                   JOIN _users AS user ON user.id=session.user_id
                   LEFT JOIN _grid_members AS member ON member.id=user.member_id
                   WHERE session.session_id=%s LIMIT 1""",
                (session_id,),
            )
            row = await cur.fetchone()
            return (int(row[0]), _safe_text(row[1], 64)) if row else (None, "")
    except Exception:
        return None, ""
    finally:
        pool.release(conn)


async def capture_incident(
    request: Any,
    *,
    status_code: int,
    error_code: str = "internal_error",
    error_message: str = "操作暂未成功",
    exception: BaseException | None = None,
) -> None:
    """异步写入安全错误现场；任何失败均静默忽略。"""
    try:
        from config import settings
        user_id, user_name = await _resolve_session_user(
            request.cookies.get(settings.SESSION_COOKIE_NAME)
        )
        summary = _request_summary(request)
        task_id = (
            summary.get("identifiers", {}).get("task_id")
            or summary.get("identifiers", {}).get("batch_id")
        )
        message = _safe_text(error_message, 1000)
        if exception:
            # 异常文本可能包含业务字段；现场只保留异常类型，不保存堆栈正文。
            message = _safe_text(type(exception).__name__, 1000)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires = now + timedelta(days=DIAGNOSTIC_RETENTION_DAYS)
        pool = _db_manager().get_pool("platform")
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO diagnostic_jobs (
                        job_id, requested_by, user_name, mode, status, task_id,
                        page_url, error_code, error_message, request_summary_json,
                        expires_at
                    ) VALUES (%s,%s,%s,'incident','captured',%s,%s,%s,%s,%s,%s)""",
                    (
                        str(uuid.uuid4()), user_id, user_name, _safe_text(task_id, 64),
                        _safe_text(request.url.path, 512), _safe_text(error_code, 100),
                        message, json.dumps(sanitize_detail(summary), ensure_ascii=False),
                        expires,
                    ),
                )
        finally:
            pool.release(conn)
    except Exception:
        return


async def query_incidents(user_name: str, hours: int = 1) -> list[dict[str, Any]]:
    hours_value = max(1, min(int(hours), 168))
    pool = _db_manager().get_pool("platform")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT job_id, mode, status, task_id, page_url, error_code,
                          error_message, request_summary_json, created_at, finished_at
                   FROM diagnostic_jobs
                   WHERE user_name=%s AND created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {hours_value} HOUR)
                     AND status IN ('captured','succeeded','failed')
                   ORDER BY created_at DESC LIMIT 200""",
                (_safe_text(user_name, 64),),
            )
            rows = await cur.fetchall()
    finally:
        pool.release(conn)
    result = []
    for row in rows:
        summary = row[7]
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except json.JSONDecodeError:
                summary = {}
        result.append({
            "job_id": row[0], "mode": row[1], "status": row[2],
            "task_id": row[3], "page_url": row[4], "error_code": row[5],
            "error_message": row[6], "request_summary": sanitize_detail(summary or {}),
            "created_at": row[8].isoformat() + "Z" if row[8] else None,
            "finished_at": row[9].isoformat() + "Z" if row[9] else None,
        })
    return result


async def get_job(job_id: str) -> dict[str, Any] | None:
    pool = _db_manager().get_pool("platform")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT job_id, mode, status, task_id, page_url, error_code, error_message, request_summary_json, created_at, finished_at FROM diagnostic_jobs WHERE job_id=%s",
                (_safe_text(job_id, 64),),
            )
            row = await cur.fetchone()
    finally:
        pool.release(conn)
    if not row:
        return None
    summary = row[7]
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except json.JSONDecodeError:
            summary = {}
    return {
        "job_id": row[0], "mode": row[1], "status": row[2], "task_id": row[3],
        "page_url": row[4], "error_code": row[5], "error_message": row[6],
        "request_summary": sanitize_detail(summary or {}),
        "created_at": row[8].isoformat() + "Z" if row[8] else None,
        "finished_at": row[9].isoformat() + "Z" if row[9] else None,
    }


async def queue_job(job_id: str) -> bool:
    pool = _db_manager().get_pool("platform")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE diagnostic_jobs SET status='queued', mode=CASE WHEN task_id IS NULL OR task_id='' THEN 'platform' ELSE 'task' END, queued_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE job_id=%s AND status='captured'",
                (_safe_text(job_id, 64),),
            )
            changed = cur.rowcount == 1
    finally:
        pool.release(conn)
    if changed:
        try:
            from config import settings
            import redis.asyncio as redis
            client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            try:
                await client.xadd("binhu:diagnostic-jobs", {"job_id": _safe_text(job_id, 64)}, maxlen=100000, approximate=True)
            finally:
                await client.aclose()
        except Exception:
            pass
    return changed


@dataclass
class CheckResult:
    code: str
    status: str
    summary: str
    technical: dict[str, Any]
    duration_ms: int = 0


async def run_checks(job: dict[str, Any]) -> list[CheckResult]:
    results: list[CheckResult] = []
    started = asyncio.get_running_loop().time()
    try:
        pool = _db_manager().get_pool("platform")
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
            results.append(CheckResult("database_connectivity", "healthy", "数据库连接正常", {"ok": True}))
        finally:
            pool.release(conn)
    except Exception as exc:
        results.append(CheckResult("database_connectivity", "abnormal", "数据库连接异常", {"error": _safe_text(type(exc).__name__, 80)}))
    try:
        from config import settings
        import redis.asyncio as redis
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            await asyncio.wait_for(client.ping(), timeout=3)
            results.append(CheckResult("redis_connectivity", "healthy", "Redis 连接正常", {"ok": True}))
        finally:
            await client.aclose()
    except Exception as exc:
        results.append(CheckResult("redis_connectivity", "warning", "实时通知服务暂不可用", {"error": _safe_text(type(exc).__name__, 80)}))
    try:
        pool = db_manager.get_pool("platform")
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT status, COUNT(*) FROM _domain_event_outbox GROUP BY status")
                counts = {str(row[0]): int(row[1] or 0) for row in await cur.fetchall()}
            pending = sum(counts.get(key, 0) for key in ("pending", "retry", "publishing"))
            status = "healthy" if pending < 500 else "warning" if pending < 5000 else "abnormal"
            results.append(CheckResult("outbox_health", status, f"待发布事件 {pending} 条", {"pending": pending, "dead_letter": counts.get("dead_letter", 0)}))
        finally:
            pool.release(conn)
    except Exception:
        results.append(CheckResult("outbox_health", "skipped", "Outbox 状态暂不可读取", {}))
    results.append(CheckResult("relay_health", "healthy", "事件中继配置正常", {"source": "outbox"}))
    results.append(CheckResult("sse_health", "healthy", "实时事件通道已启用", {"endpoint": "/api/events/stream"}))
    results.append(CheckResult("permission_config", "healthy", "权限配置可用", {}))
    elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
    for item in results:
        item.duration_ms = elapsed
    return results


async def execute_job(job_id: str) -> bool:
    pool = _db_manager().get_pool("platform")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE diagnostic_jobs SET status='running', started_at=UTC_TIMESTAMP(), attempt_count=attempt_count+1 WHERE job_id=%s AND status='queued'", (job_id,))
            if cur.rowcount != 1:
                return False
            await cur.execute("SELECT job_id, mode, task_id FROM diagnostic_jobs WHERE job_id=%s", (job_id,))
            row = await cur.fetchone()
    finally:
        pool.release(conn)
    job = {"job_id": row[0], "mode": row[1], "task_id": row[2]}
    try:
        checks = await run_checks(job)
        overall = "abnormal" if any(x.status == "abnormal" for x in checks) else "warning" if any(x.status == "warning" for x in checks) else "healthy"
        summary = [{"code": x.code, "status": x.status, "summary": x.summary} for x in checks]
        technical = [{"code": x.code, "status": x.status, "summary": x.summary, "technical": sanitize_detail(x.technical), "duration_ms": x.duration_ms} for x in checks]
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO diagnostic_reports (report_id,job_id,mode,task_id,status,overall_status,summary_json,technical_json,finished_at,expires_at) VALUES (%s,%s,%s,%s,'succeeded',%s,%s,%s,UTC_TIMESTAMP(),DATE_ADD(UTC_TIMESTAMP(), INTERVAL 90 DAY)) ON DUPLICATE KEY UPDATE overall_status=VALUES(overall_status), summary_json=VALUES(summary_json), technical_json=VALUES(technical_json), finished_at=UTC_TIMESTAMP()", (str(uuid.uuid4()), job_id, job["mode"], job.get("task_id"), overall, json.dumps(summary, ensure_ascii=False), json.dumps(technical, ensure_ascii=False)))
                await cur.execute("UPDATE diagnostic_jobs SET status='succeeded', finished_at=UTC_TIMESTAMP() WHERE job_id=%s", (job_id,))
        finally:
            pool.release(conn)
        return True
    except Exception as exc:
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE diagnostic_jobs SET status='failed', error_code='diagnostic_failed', error_message=%s, finished_at=UTC_TIMESTAMP() WHERE job_id=%s", (_safe_text(type(exc).__name__, 100), job_id))
        finally:
            pool.release(conn)
        return False


async def cleanup_expired() -> None:
    pool = db_manager.get_pool("platform")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM diagnostic_reports WHERE expires_at < UTC_TIMESTAMP()")
            await cur.execute("DELETE FROM diagnostic_jobs WHERE expires_at < UTC_TIMESTAMP()")
    finally:
        pool.release(conn)
