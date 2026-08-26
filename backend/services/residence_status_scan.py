"""Persistent background lookup of residence registration state for mobile tasks."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from services.qmf_registration import normalize_identity, valid_identity
from services.residence_platform import ResidencePlatformClient, ResidencePlatformError
from services.residence_platform_config import load_residence_config
from services.task_workflow import MOBILE_TASK_TYPES, TASK_WORKFLOWS


LOOKUP_BATCH_SIZE = 50
LOOKUP_CONCURRENCY = 2
REFRESH_DAYS = 7
_wake_event = asyncio.Event()


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
                "SET status='pending',error_code='interrupted' "
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
                            error_code=VALUES(error_code),checked_at=NULL,last_attempt_at=NULL
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
                        identity_hmac=VALUES(identity_hmac)
                    """,
                    (parser_type, row_key, identity_hmac, status, error_code),
                )
                queued += int(valid)
            await cur.execute(
                """
                UPDATE _residence_registration_status
                SET status='pending',error_code='',last_attempt_at=NULL
                WHERE status IN ('registered','first_registration')
                  AND checked_at<DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
                """
            )
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


async def _load_current_identity(parser_type: str, row_key: str, expected_hmac: str) -> str:
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
    identity = _identity(parser_type, row[0])
    if not valid_identity(identity):
        raise ResidencePlatformError("invalid_identity", "身份证号码无效")
    return identity


async def _save_result(
    parser_type: str,
    row_key: str,
    *,
    status: str,
    error_code: str = "",
) -> None:
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _residence_registration_status "
                "SET status=%s,error_code=%s,checked_at=UTC_TIMESTAMP() "
                "WHERE parser_type=%s AND row_key=%s",
                (status, error_code[:64], parser_type, row_key),
            )
        await conn.commit()


async def _process_one(
    client: ResidencePlatformClient,
    item: tuple[str, str, str],
) -> str:
    parser_type, row_key, identity_hmac = item
    try:
        identity = await _load_current_identity(parser_type, row_key, identity_hmac)
        result = await client.lookup(identity)
        await _save_result(
            parser_type,
            row_key,
            status=result.state,
            error_code=result.error_code,
        )
        return result.error_code
    except ResidencePlatformError as exc:
        status = "pending" if exc.code == "source_changed" else "error"
        await _save_result(parser_type, row_key, status=status, error_code=exc.code)
        return exc.code
    except Exception:  # noqa: BLE001 - external response details must not reach logs
        await _save_result(parser_type, row_key, status="error", error_code="request_error")
        return "request_error"


async def run_residence_lookup_cycle(*, queue_tasks: bool = True) -> dict[str, int | str]:
    pool = _pool()
    async with pool.acquire() as conn:
        config = await load_residence_config(conn)
    if not config.session_ready:
        return {"processed": 0, "status": "session_not_ready"}
    if queue_tasks:
        await queue_due_residence_tasks()
    items = await _claim_pending(LOOKUP_BATCH_SIZE)
    if not items:
        return {"processed": 0, "status": "idle"}
    client = ResidencePlatformClient(config)
    semaphore = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def guarded(item):
        async with semaphore:
            return await _process_one(client, item)

    errors = await asyncio.gather(*(guarded(item) for item in items))
    if "authentication_expired" in errors:
        return {"processed": len(items), "status": "authentication_expired"}
    return {"processed": len(items), "status": "completed"}


def wake_residence_lookup_scheduler() -> None:
    _wake_event.set()


async def run_residence_lookup_scheduler() -> None:
    while True:
        try:
            queue_tasks = True
            while True:
                result = await run_residence_lookup_cycle(queue_tasks=queue_tasks)
                queue_tasks = False
                if int(result.get("processed") or 0) <= 0:
                    break
                if result.get("status") == "authentication_expired":
                    break
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - safe type-only diagnostic
            print(f"[RESIDENCE_LOOKUP] cycle failed: {type(exc).__name__}")
        try:
            await asyncio.wait_for(_wake_event.wait(), timeout=60)
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
               status.checked_at,status.last_attempt_at
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
    for row_key, identity_hmac, status, error_code, checked_at, last_attempt_at in await cur.fetchall():
        key = str(row_key)
        state = str(status or "pending")
        result[key] = {
            "state": state,
            "checked_at": checked_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if isinstance(checked_at, datetime) else None,
            "last_attempt_at": last_attempt_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if isinstance(last_attempt_at, datetime) else None,
            "error_code": str(error_code or ""),
        }
    return result
