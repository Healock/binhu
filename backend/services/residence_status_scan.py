"""Persistent background lookup of residence registration state for mobile tasks."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from services.qmf_registration import normalize_identity, valid_identity
from services.qmf_community import normalize_qmf_community_code, valid_qmf_community_code
from services.residence_platform import ResidencePlatformClient, ResidencePlatformError
from services.residence_platform_config import (
    ResidenceCommunitySession,
    ResidencePlatformConfig,
    load_residence_config,
    load_residence_session,
    residence_username,
    save_residence_session,
)
from services.police_dispatch import normalize_community_label
from services.parsers import get_parser
from services.task_workflow import MOBILE_TASK_TYPES, TASK_WORKFLOWS

if TYPE_CHECKING:
    from services.external_acquisition_jobs import JobContext


LOOKUP_BATCH_SIZE = 50
LOOKUP_CONCURRENCY = 2
_wake_event = asyncio.Event()
_force_full_scan_requested = False
_community_login_locks: dict[str, asyncio.Lock] = {}
_scan_lock = asyncio.Lock()


@dataclass(frozen=True)
class ResidenceLookupTarget:
    identity: str
    community_code: str


def _pool():
    from database import db_manager

    return db_manager.get_pool("online_data")


async def ensure_residence_status_schema(cur) -> None:
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _residence_registration_status (
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            identity_hmac CHAR(64) NOT NULL DEFAULT '',
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            error_code VARCHAR(64) NOT NULL DEFAULT '',
            checked_at DATETIME DEFAULT NULL,
            last_attempt_at DATETIME DEFAULT NULL,
            duration_ms INT UNSIGNED DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (parser_type,row_key),
            INDEX idx_residence_status_queue (status,last_attempt_at),
            INDEX idx_residence_status_checked (checked_at),
            INDEX idx_residence_status_identity (identity_hmac)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )
    await cur.execute(
        "SHOW COLUMNS FROM _residence_registration_status LIKE 'duration_ms'"
    )
    if not await cur.fetchone():
        await cur.execute(
            "ALTER TABLE _residence_registration_status "
            "ADD COLUMN duration_ms INT UNSIGNED DEFAULT NULL AFTER last_attempt_at"
        )


def _values(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value or "") for key, value in raw.items()}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return (
        {str(key): str(value or "") for key, value in parsed.items()}
        if isinstance(parsed, dict)
        else {}
    )


def _identity(parser_type: str, raw_values: Any) -> str:
    workflow = TASK_WORKFLOWS.get(parser_type)
    if not workflow:
        return ""
    return normalize_identity(workflow.first_value(_values(raw_values), workflow.identity_fields))


async def queue_due_residence_tasks(*, force: bool = False) -> int:
    """Create or refresh safe status rows without storing identity plaintext."""
    pool = _pool()
    parser_placeholders = ",".join(["%s"] * len(MOBILE_TASK_TYPES))
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _residence_registration_status "
                "SET status='pending',error_code='interrupted',duration_ms=NULL "
                "WHERE status='querying' "
                "AND last_attempt_at<DATE_SUB(UTC_TIMESTAMP(), INTERVAL 5 MINUTE)"
            )
            await cur.execute(
                f"""
                SELECT parser_type,row_key,values_json,COALESCE(identity_hmac,'')
                FROM _online_source_projection
                WHERE parser_type IN ({parser_placeholders})
                """,
                MOBILE_TASK_TYPES,
            )
            rows = await cur.fetchall()
            queued = 0
            for parser_type, row_key, raw_values, identity_hmac in rows:
                identity = _identity(str(parser_type), raw_values)
                valid = valid_identity(identity)
                status = "pending" if valid else "error"
                error_code = "" if valid else "invalid_identity"
                if force:
                    await cur.execute(
                        """
                        INSERT INTO _residence_registration_status
                            (parser_type,row_key,identity_hmac,status,error_code,checked_at,last_attempt_at)
                        VALUES (%s,%s,%s,%s,%s,NULL,NULL)
                        ON DUPLICATE KEY UPDATE
                            identity_hmac=VALUES(identity_hmac),status=VALUES(status),
                            error_code=VALUES(error_code),checked_at=NULL,
                            last_attempt_at=NULL,duration_ms=NULL
                        """,
                        (parser_type, row_key, identity_hmac, status, error_code),
                    )
                    queued += int(valid)
                    continue
                await cur.execute(
                    """
                    INSERT INTO _residence_registration_status
                        (parser_type,row_key,identity_hmac,status,error_code)
                    VALUES (%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        status=IF(identity_hmac<>VALUES(identity_hmac),'pending',status),
                        error_code=IF(identity_hmac<>VALUES(identity_hmac),'',error_code),
                        checked_at=IF(identity_hmac<>VALUES(identity_hmac),NULL,checked_at),
                        last_attempt_at=IF(identity_hmac<>VALUES(identity_hmac),NULL,last_attempt_at),
                        duration_ms=IF(identity_hmac<>VALUES(identity_hmac),NULL,duration_ms),
                        identity_hmac=VALUES(identity_hmac)
                    """,
                    (parser_type, row_key, identity_hmac, status, error_code),
                )
                queued += int(valid)
        await conn.commit()
    return queued


async def _claim_pending(limit: int) -> list[tuple[str, str, str]]:
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status.parser_type,status.row_key,status.identity_hmac
                FROM _residence_registration_status AS status
                JOIN _online_source_projection AS projection
                  ON projection.parser_type=status.parser_type
                 AND projection.row_key=status.row_key
                WHERE status.status='pending'
                  AND (status.last_attempt_at IS NULL
                       OR status.last_attempt_at<DATE_SUB(UTC_TIMESTAMP(), INTERVAL 5 MINUTE))
                ORDER BY status.updated_at,status.parser_type,status.row_key
                LIMIT %s
                """,
                (limit,),
            )
            rows = [(str(row[0]), str(row[1]), str(row[2] or "")) for row in await cur.fetchall()]
            for parser_type, row_key, _ in rows:
                await cur.execute(
                    "UPDATE _residence_registration_status "
                    "SET status='querying',last_attempt_at=UTC_TIMESTAMP(),error_code='' "
                    "WHERE parser_type=%s AND row_key=%s AND status='pending'",
                    (parser_type, row_key),
                )
        await conn.commit()
    return rows


async def _resolve_community_code(cur, source_community: str) -> str:
    source_key = normalize_community_label(source_community)
    if not source_key:
        raise ResidencePlatformError("community_missing", "任务社区未填写")
    await cur.execute(
        """
        SELECT community.id,community.name,community.qmf_community_code,alias.alias
        FROM _communities AS community
        LEFT JOIN _community_aliases AS alias ON alias.community_id=community.id
        WHERE community.is_active=1
        ORDER BY community.id,alias.id
        """
    )
    matches: dict[int, str] = {}
    for community_id, name, community_code, alias in await cur.fetchall():
        labels = (str(name or ""), str(alias or ""))
        if any(normalize_community_label(label) == source_key for label in labels if label):
            matches[int(community_id)] = normalize_qmf_community_code(community_code)
    if not matches:
        raise ResidencePlatformError("community_not_found", "任务社区无法匹配社区管理")
    if len(matches) != 1:
        raise ResidencePlatformError("community_ambiguous", "任务社区匹配到多个社区")
    code = next(iter(matches.values()))
    if not valid_qmf_community_code(code):
        raise ResidencePlatformError("community_code_missing", "社区尚未配置全民防社区代码")
    return code


async def _load_current_target(
    parser_type: str,
    row_key: str,
    expected_hmac: str,
) -> ResidenceLookupTarget:
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT values_json,COALESCE(identity_hmac,'') "
                "FROM _online_source_projection WHERE parser_type=%s AND row_key=%s",
                (parser_type, row_key),
            )
            row = await cur.fetchone()
            if not row or str(row[1] or "") != expected_hmac:
                raise ResidencePlatformError("source_changed", "任务来源已变化")
            values = _values(row[0])
            identity = _identity(parser_type, values)
            if not valid_identity(identity):
                raise ResidencePlatformError("invalid_identity", "身份证号码无效")
            parser = get_parser(parser_type)
            community_code = await _resolve_community_code(
                cur,
                parser.community_value(values),
            )
    return ResidenceLookupTarget(identity=identity, community_code=community_code)


async def _community_client(
    config: ResidencePlatformConfig,
    community_code: str,
    *,
    rejected_token: str = "",
) -> ResidencePlatformClient:
    pool = _pool()
    async with pool.acquire() as conn:
        session = await load_residence_session(conn, community_code)
    if session is None or session.token == rejected_token:
        lock = _community_login_locks.setdefault(community_code, asyncio.Lock())
        async with lock:
            async with pool.acquire() as conn:
                session = await load_residence_session(conn, community_code)
            if session is None or session.token == rejected_token:
                login_config = replace(
                    config,
                    username=residence_username(community_code),
                    access_token="",
                    organization_code="",
                )
                token, detected_org = await ResidencePlatformClient(login_config).login()
                organization_code = detected_org or community_code[:6]
                session = ResidenceCommunitySession(
                    token=token,
                    organization_code=organization_code,
                )
                async with pool.acquire() as conn:
                    await save_residence_session(conn, community_code, session)
    return ResidencePlatformClient(replace(
        config,
        username=residence_username(community_code),
        access_token=session.token,
        organization_code=session.organization_code,
    ))


async def _lookup_target(
    config: ResidencePlatformConfig,
    target: ResidenceLookupTarget,
) -> Any:
    client = await _community_client(config, target.community_code)
    try:
        return await client.lookup(target.identity)
    except ResidencePlatformError as exc:
        if exc.code != "authentication_expired":
            raise
    client = await _community_client(
        config,
        target.community_code,
        rejected_token=client.config.access_token,
    )
    return await client.lookup(target.identity)


async def _save_result(
    parser_type: str,
    row_key: str,
    *,
    status: str,
    error_code: str = "",
    duration_ms: int | None = None,
) -> None:
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _residence_registration_status "
                "SET status=%s,error_code=%s,duration_ms=%s,checked_at=UTC_TIMESTAMP() "
                "WHERE parser_type=%s AND row_key=%s",
                (
                    status,
                    error_code[:64],
                    max(0, int(duration_ms)) if duration_ms is not None else None,
                    parser_type,
                    row_key,
                ),
            )
        await conn.commit()


async def _process_one(
    config: ResidencePlatformConfig,
    item: tuple[str, str, str],
) -> tuple[str, str]:
    parser_type, row_key, identity_hmac = item
    started = time.perf_counter()
    try:
        target = await _load_current_target(parser_type, row_key, identity_hmac)
        result = await _lookup_target(config, target)
        await _save_result(
            parser_type,
            row_key,
            status=result.state,
            error_code=result.error_code,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return result.state, result.error_code
    except ResidencePlatformError as exc:
        status = "pending" if exc.code == "source_changed" else "error"
        await _save_result(
            parser_type,
            row_key,
            status=status,
            error_code=exc.code,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return status, exc.code
    except Exception:  # noqa: BLE001 - external response details must not reach logs
        await _save_result(
            parser_type,
            row_key,
            status="error",
            error_code="request_error",
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return "error", "request_error"


async def run_residence_lookup_cycle(
    *,
    queue_tasks: bool = True,
    full_scan: bool = False,
) -> dict[str, int | str]:
    pool = _pool()
    async with pool.acquire() as conn:
        config = await load_residence_config(conn)
    if not config.session_ready:
        return {"processed": 0, "status": "session_not_ready"}
    if queue_tasks:
        await queue_due_residence_tasks(force=full_scan)
    items = await _claim_pending(LOOKUP_BATCH_SIZE)
    if not items:
        return {"processed": 0, "status": "idle"}
    semaphore = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def guarded(item):
        async with semaphore:
            return await _process_one(config, item)

    outcomes = await asyncio.gather(*(guarded(item) for item in items))
    success_count = sum(not error_code for _, error_code in outcomes)
    error_count = len(outcomes) - success_count
    if any(error_code == "authentication_expired" for _, error_code in outcomes):
        return {
            "processed": len(items),
            "success_count": success_count,
            "error_count": error_count,
            "status": "authentication_expired",
        }
    return {
        "processed": len(items),
        "success_count": success_count,
        "error_count": error_count,
        "status": "completed",
    }


async def run_residence_full_scan_job(context: "JobContext") -> dict[str, Any]:
    """Drain one manually requested full scan with safe aggregate progress only."""
    async with _scan_lock:
        pool = _pool()
        async with pool.acquire() as conn:
            config = await load_residence_config(conn)
        if not config.session_ready:
            raise RuntimeError("居住证平台配置尚未就绪")

        total = await queue_due_residence_tasks(force=True)
        await context.update(
            phase="preparing",
            current=0,
            total=total,
            message=f"已准备 {total} 条任务，等待查询",
        )
        processed = 0
        success_count = 0
        error_count = 0
        stopped_for_authentication = False
        while True:
            result = await run_residence_lookup_cycle(queue_tasks=False)
            batch_count = int(result.get("processed") or 0)
            if batch_count <= 0:
                break
            processed += batch_count
            success_count += int(result.get("success_count") or 0)
            error_count += int(result.get("error_count") or 0)
            await context.update(
                phase="querying",
                current=processed,
                total=total,
                message=f"正在查询：已处理 {processed}/{total} 条",
            )
            if result.get("status") == "authentication_expired":
                stopped_for_authentication = True
                break
            await asyncio.sleep(0)

        message = f"查询完成：成功 {success_count} 条，异常 {error_count} 条"
        if stopped_for_authentication:
            message = f"登录状态失效，已停止：成功 {success_count} 条，异常 {error_count} 条"
        return {
            "status": "warning" if error_count or stopped_for_authentication else "success",
            "processed": processed,
            "success_count": success_count,
            "error_count": error_count,
            "message": message,
        }


def wake_residence_lookup_scheduler(*, force_full_scan: bool = False) -> None:
    global _force_full_scan_requested
    if force_full_scan:
        _force_full_scan_requested = True
    _wake_event.set()


async def run_residence_lookup_scheduler() -> None:
    global _force_full_scan_requested
    next_full_scan_at = 0.0
    while True:
        try:
            pool = _pool()
            async with pool.acquire() as conn:
                config = await load_residence_config(conn)
            force_full_scan = _force_full_scan_requested
            _force_full_scan_requested = False
            full_scan_due = bool(
                config.session_ready
                and (force_full_scan or time.monotonic() >= next_full_scan_at)
            )
            async with _scan_lock:
                queue_tasks = True
                while True:
                    result = await run_residence_lookup_cycle(
                        queue_tasks=queue_tasks,
                        full_scan=full_scan_due and queue_tasks,
                    )
                    queue_tasks = False
                    if int(result.get("processed") or 0) <= 0:
                        break
                    if result.get("status") == "authentication_expired":
                        break
                    await asyncio.sleep(0)
            if full_scan_due:
                next_full_scan_at = (
                    time.monotonic() + config.full_scan_interval_minutes * 60
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - safe type-only diagnostic
            print(f"[RESIDENCE_LOOKUP] cycle failed: {type(exc).__name__}")
        try:
            wait_seconds = 60.0
            if next_full_scan_at > 0:
                wait_seconds = max(1.0, next_full_scan_at - time.monotonic())
            await asyncio.wait_for(_wake_event.wait(), timeout=wait_seconds)
            _wake_event.clear()
        except asyncio.TimeoutError:
            pass


async def residence_status_by_rows(
    cur,
    parser_type: str,
    rows: list[tuple],
) -> dict[str, dict[str, Any]]:
    if parser_type not in MOBILE_TASK_TYPES or not rows:
        return {}
    keys = [str(row[0]) for row in rows]
    placeholders = ",".join(["%s"] * len(keys))
    await cur.execute(
        f"""
        SELECT status.row_key,status.identity_hmac,status.status,status.error_code,
               status.checked_at,status.last_attempt_at,status.duration_ms
        FROM _residence_registration_status AS status
        JOIN _online_source_projection AS projection
          ON projection.parser_type=status.parser_type
         AND projection.row_key=status.row_key
         AND COALESCE(projection.identity_hmac,'')=status.identity_hmac
        WHERE status.parser_type=%s AND status.row_key IN ({placeholders})
        """,
        (parser_type, *keys),
    )
    result: dict[str, dict[str, Any]] = {}
    for row_key, identity_hmac, status, error_code, checked_at, last_attempt_at, duration_ms in await cur.fetchall():
        key = str(row_key)
        state = str(status or "pending")
        result[key] = {
            "state": state,
            "checked_at": checked_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if isinstance(checked_at, datetime) else None,
            "last_attempt_at": last_attempt_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if isinstance(last_attempt_at, datetime) else None,
            "error_code": str(error_code or ""),
            "duration_ms": int(duration_ms) if duration_ms is not None else None,
        }
    return result
