"""Persistent read-only reconciliation of completed model-three tasks."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.qmf_config import load_qmf_config
from services.qmf_registration import (
    MODEL_THREE_PARSER,
    normalize_identity,
    valid_identity,
)
from services.qmf_status import (
    QmfLegacyStatus,
    QmfLegacyStatusClient,
    QmfStatusAccessError,
    STATUS_COMPLETED_MATCH,
    STATUS_COMPLETED_MISMATCH,
    STATUS_NON_JURISDICTION,
    STATUS_UNAVAILABLE,
    normalize_qmf_status_result,
)
from services.online_source import active_source_sql_filter
from services.task_graph import reconcile_projection_task_graph


SCAN_CONCURRENCY = 4
STALE_DAYS = 7
FAILURE_CIRCUIT_LIMIT = 20
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_ITEM_STATUSES = ("completed", "failed")
_SCAN_LOCK = "binhu:qmf-status-scan"
_SCHEDULE_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

_background_tasks: set[asyncio.Task] = set()


def _pool():
    from database import db_manager

    return db_manager.get_pool("online_data")


def valid_schedule_time(value: str) -> bool:
    return bool(_SCHEDULE_TIME.fullmatch(str(value or "").strip()))


def _utc_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    return str(value)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _state_bucket(state: str) -> str:
    if state == STATUS_COMPLETED_MATCH:
        return "match_count"
    if state == STATUS_COMPLETED_MISMATCH:
        return "mismatch_count"
    if state == "pending":
        return "pending_count"
    if state == "not_found":
        return "not_found_count"
    if state == STATUS_NON_JURISDICTION:
        return "non_jurisdiction_count"
    return "error_count"


async def archive_due_qmf_tasks() -> int:
    """归档已连续保持一致一整天的模型三任务，且全过程幂等。"""
    pool = _pool()
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT snapshot.row_key FROM _qmf_status_snapshots AS snapshot "
                    "JOIN _online_source_projection AS projection "
                    " ON projection.parser_type=snapshot.parser_type AND projection.row_key=snapshot.row_key "
                    "WHERE snapshot.parser_type=%s AND snapshot.feedback_state='completed_match' "
                    "AND snapshot.archived_at IS NULL AND snapshot.archive_due_at IS NOT NULL "
                    "AND snapshot.archive_due_at<=UTC_TIMESTAMP() AND projection.task_state='completed' "
                    "FOR UPDATE",
                    (MODEL_THREE_PARSER,),
                )
                keys = [str(row[0]) for row in await cur.fetchall() if row[0]]
                if not keys:
                    await conn.commit()
                    return 0
                placeholders = ",".join(["%s"] * len(keys))
                columns = ["截止时间", "核查人", "姓名", "身份证号", "联系方式", "地址", "下发社区", "核查结果", "备注"]
                quoted = ",".join(f"`{column}`" for column in columns)
                await cur.execute(
                    f"INSERT IGNORE INTO OnlineDataArchive.t_suspect_unrevoked_archive "
                    f"(_row_key,{quoted},_archive_reason) "
                    f"SELECT _row_key,{quoted},'qmf_feedback_match' FROM t_suspect_unrevoked "
                    f"WHERE _row_key IN ({placeholders})",
                    keys,
                )
                await cur.execute(
                    f"DELETE FROM t_suspect_unrevoked WHERE _row_key IN ({placeholders})", keys
                )
                await cur.execute(
                    f"DELETE FROM _online_source_rows WHERE parser_type=%s AND row_key IN ({placeholders})",
                    (MODEL_THREE_PARSER, *keys),
                )
                await cur.execute(
                    f"DELETE FROM _online_source_projection WHERE parser_type=%s AND row_key IN ({placeholders})",
                    (MODEL_THREE_PARSER, *keys),
                )
                await reconcile_projection_task_graph(cur, MODEL_THREE_PARSER)
                await cur.execute(
                    f"UPDATE _qmf_status_snapshots SET archived_at=UTC_TIMESTAMP() "
                    f"WHERE parser_type=%s AND row_key IN ({placeholders})",
                    (MODEL_THREE_PARSER, *keys),
                )
            await conn.commit()
            return len(keys)
        except Exception:
            await conn.rollback()
            raise


async def ensure_qmf_status_scan_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _qmf_status_scan_runs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            trigger_source VARCHAR(20) NOT NULL,
            scan_mode VARCHAR(20) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'queued',
            concurrency INT NOT NULL DEFAULT 4,
            total_count INT NOT NULL DEFAULT 0,
            processed_count INT NOT NULL DEFAULT 0,
            match_count INT NOT NULL DEFAULT 0,
            mismatch_count INT NOT NULL DEFAULT 0,
            pending_count INT NOT NULL DEFAULT 0,
            not_found_count INT NOT NULL DEFAULT 0,
            non_jurisdiction_count INT NOT NULL DEFAULT 0,
            error_count INT NOT NULL DEFAULT 0,
            requested_by INT DEFAULT NULL,
            scheduled_date DATE DEFAULT NULL,
            error_code VARCHAR(64) NOT NULL DEFAULT '',
            started_at DATETIME DEFAULT NULL,
            finished_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_qmf_status_scan_schedule (
                trigger_source, scheduled_date
            ),
            INDEX idx_qmf_status_scan_status (status, id),
            INDEX idx_qmf_status_scan_created (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _qmf_status_scan_items (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            run_id BIGINT NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            source_id BIGINT NOT NULL,
            expected_revision BIGINT UNSIGNED NOT NULL,
            expected_row_hash CHAR(64) NOT NULL,
            identity_hmac CHAR(64) NOT NULL DEFAULT '',
            expected_result VARCHAR(30) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            feedback_state VARCHAR(40) NOT NULL DEFAULT '',
            feedback_result VARCHAR(30) NOT NULL DEFAULT '',
            checked_at VARCHAR(64) NOT NULL DEFAULT '',
            origin VARCHAR(40) NOT NULL DEFAULT '',
            error_code VARCHAR(64) NOT NULL DEFAULT '',
            duration_ms INT NOT NULL DEFAULT 0,
            started_at DATETIME DEFAULT NULL,
            finished_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_qmf_status_scan_item (run_id, parser_type, row_key),
            INDEX idx_qmf_status_scan_item_queue (run_id, status, id),
            INDEX idx_qmf_status_scan_item_source (source_id, id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute(
        "SHOW COLUMNS FROM `_qmf_status_scan_runs` WHERE Field=%s",
        ("non_jurisdiction_count",),
    )
    if not await cur.fetchone():
        await cur.execute(
            "ALTER TABLE `_qmf_status_scan_runs` "
            "ADD COLUMN non_jurisdiction_count INT NOT NULL DEFAULT 0 "
            "AFTER not_found_count"
        )
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _qmf_status_snapshots (
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            source_id BIGINT NOT NULL,
            source_revision BIGINT UNSIGNED NOT NULL,
            source_row_hash CHAR(64) NOT NULL,
            identity_hmac CHAR(64) NOT NULL DEFAULT '',
            platform_result VARCHAR(30) NOT NULL DEFAULT '',
            feedback_state VARCHAR(40) NOT NULL DEFAULT '',
            feedback_result VARCHAR(30) NOT NULL DEFAULT '',
            checked_at VARCHAR(64) NOT NULL DEFAULT '',
            origin VARCHAR(40) NOT NULL DEFAULT '',
            error_code VARCHAR(64) NOT NULL DEFAULT '',
            scan_run_id BIGINT NOT NULL,
            matched_at DATETIME DEFAULT NULL,
            archive_due_at DATETIME DEFAULT NULL,
            archived_at DATETIME DEFAULT NULL,
            last_scanned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (parser_type, row_key),
            INDEX idx_qmf_status_snapshot_state (
                parser_type, feedback_state, last_scanned_at
            ),
            INDEX idx_qmf_status_snapshot_source (source_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    for column, definition in (
        ("matched_at", "DATETIME DEFAULT NULL AFTER scan_run_id"),
        ("archive_due_at", "DATETIME DEFAULT NULL AFTER matched_at"),
        ("archived_at", "DATETIME DEFAULT NULL AFTER archive_due_at"),
    ):
        await cur.execute(
            "SHOW COLUMNS FROM `_qmf_status_snapshots` WHERE Field=%s", (column,)
        )
        if not await cur.fetchone():
            await cur.execute(
                f"ALTER TABLE `_qmf_status_snapshots` ADD COLUMN {column} {definition}"
            )


async def _acquire_lock(cur) -> bool:
    await cur.execute("SELECT GET_LOCK(%s, 0)", (_SCAN_LOCK,))
    row = await cur.fetchone()
    return bool(row and int(row[0] or 0) == 1)


async def _release_lock(cur) -> None:
    await cur.execute("SELECT RELEASE_LOCK(%s)", (_SCAN_LOCK,))


async def create_status_scan_run(
    *,
    trigger_source: str,
    requested_by: int | None,
    scheduled_date=None,
) -> tuple[int, int]:
    """Freeze a full or incremental target set and return run id/count."""
    scan_mode = "full" if trigger_source == "manual" else "incremental"
    pool = _pool()
    conn = await pool.acquire()
    lock_acquired = False
    try:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                if not await _acquire_lock(cur):
                    raise RuntimeError("scan_busy")
                lock_acquired = True
                await cur.execute(
                    "SELECT id FROM _qmf_status_scan_runs "
                    "WHERE status IN ('queued','running') LIMIT 1"
                )
                active = await cur.fetchone()
                if active:
                    raise RuntimeError("scan_busy")
                if trigger_source == "scheduled" and scheduled_date is not None:
                    await cur.execute(
                        "SELECT id FROM _qmf_status_scan_runs "
                        "WHERE trigger_source='scheduled' AND scheduled_date=%s LIMIT 1",
                        (scheduled_date,),
                    )
                    if await cur.fetchone():
                        raise RuntimeError("scan_already_scheduled")
                await cur.execute(
                    """
                    INSERT INTO _qmf_status_scan_runs (
                        trigger_source, scan_mode, status, concurrency,
                        requested_by, scheduled_date
                    ) VALUES (%s, %s, 'queued', %s, %s, %s)
                    """,
                    (
                        trigger_source,
                        scan_mode,
                        SCAN_CONCURRENCY,
                        requested_by,
                        scheduled_date,
                    ),
                )
                run_id = int(cur.lastrowid)
                incremental = "" if scan_mode == "full" else """
                        AND (
                            snapshot.row_key IS NULL
                            OR snapshot.error_code<>''
                            OR snapshot.feedback_state NOT IN (
                                'pending','completed_match',
                                'completed_mismatch','not_found'
                            )
                            OR snapshot.source_id<>source.id
                            OR snapshot.source_revision<>source.revision
                            OR snapshot.source_row_hash<>source.row_hash
                            OR snapshot.platform_result<>CASE TRIM(COALESCE(
                                JSON_UNQUOTE(JSON_EXTRACT(
                                    projection.values_json, '$.\"核查结果\"'
                                )), ''
                            ))
                                WHEN '离吴' THEN '离开不返吴'
                                WHEN '近期反吴' THEN '近期返吴'
                                ELSE TRIM(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(
                                    projection.values_json, '$.\"核查结果\"'
                                )), ''))
                            END
                            OR snapshot.last_scanned_at<DATE_SUB(
                                UTC_TIMESTAMP(), INTERVAL 7 DAY
                            )
                            OR (
                                snapshot.feedback_state='completed_match'
                                AND snapshot.archive_due_at IS NOT NULL
                                AND snapshot.archive_due_at<=UTC_TIMESTAMP()
                            )
                        )
                """
                await cur.execute(
                    f"""
                    INSERT INTO _qmf_status_scan_items (
                        run_id, parser_type, row_key, source_id,
                        expected_revision, expected_row_hash, identity_hmac,
                        expected_result
                    )
                    SELECT %s, projection.parser_type, projection.row_key,
                           source.id, source.revision, source.row_hash,
                           COALESCE(projection.identity_hmac, ''),
                           TRIM(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(
                               projection.values_json, '$.\"核查结果\"'
                           )), ''))
                    FROM _online_source_projection AS projection
                    JOIN (
                        SELECT candidate.parser_type,candidate.row_key,
                               MIN(candidate.id) AS source_id
                        FROM _online_source_rows AS candidate
                        WHERE 1=1
                        {active_source_sql_filter(MODEL_THREE_PARSER, 'candidate')}
                        GROUP BY candidate.parser_type,candidate.row_key
                    ) AS selected_source
                      ON selected_source.parser_type=projection.parser_type
                     AND selected_source.row_key=projection.row_key
                    JOIN _online_source_rows AS source
                      ON source.id=selected_source.source_id
                    LEFT JOIN _qmf_status_snapshots AS snapshot
                      ON snapshot.parser_type=projection.parser_type
                     AND snapshot.row_key=projection.row_key
                    WHERE projection.parser_type=%s
                      AND projection.task_state='completed'
                      {incremental}
                    """,
                    (run_id, MODEL_THREE_PARSER),
                )
                total = int(cur.rowcount or 0)
                await cur.execute(
                    "UPDATE _qmf_status_scan_runs SET total_count=%s, "
                    "status=IF(%s=0,'completed','queued'), "
                    "finished_at=IF(%s=0,UTC_TIMESTAMP(),NULL) WHERE id=%s",
                    (total, total, total, run_id),
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    finally:
        try:
            if lock_acquired:
                async with conn.cursor() as cur:
                    await _release_lock(cur)
        finally:
            pool.release(conn)
    if total:
        launch_status_scan(run_id)
    return run_id, total


async def _claim_item(run_id: int) -> dict[str, Any] | None:
    pool = _pool()
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id,parser_type,row_key,source_id,expected_revision,"
                "expected_row_hash,identity_hmac,expected_result "
                "FROM _qmf_status_scan_items WHERE run_id=%s AND status='pending' "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (run_id,),
            )
            row = await cur.fetchone()
            if not row:
                await conn.commit()
                return None
            await cur.execute(
                "UPDATE _qmf_status_scan_items SET status='processing', "
                "started_at=UTC_TIMESTAMP() WHERE id=%s AND status='pending'",
                (row[0],),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)
    return {
        "id": int(row[0]),
        "parser_type": str(row[1]),
        "row_key": str(row[2]),
        "source_id": int(row[3]),
        "expected_revision": int(row[4]),
        "expected_row_hash": str(row[5]),
        "identity_hmac": str(row[6] or ""),
        "expected_result": str(row[7] or ""),
    }


async def _current_item_context(item: dict[str, Any]) -> tuple[str, str]:
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT source.revision,source.row_hash,source.values_json,
                       projection.task_state,projection.source_count,
                       projection.conflict,projection.identity_hmac
                FROM _online_source_rows AS source
                JOIN _online_source_projection AS projection
                  ON projection.parser_type=source.parser_type
                 AND projection.row_key=source.row_key
                WHERE source.id=%s AND source.parser_type=%s
                  AND source.row_key=%s
                """,
                (item["source_id"], item["parser_type"], item["row_key"]),
            )
            row = await cur.fetchone()
    if not row:
        raise ValueError("source_missing")
    if (
        int(row[0]) != item["expected_revision"]
        or str(row[1]) != item["expected_row_hash"]
    ):
        raise ValueError("source_changed")
    if str(row[3] or "") != "completed":
        raise ValueError("task_not_completed")
    if int(row[4] or 0) != 1 or bool(row[5]):
        raise ValueError("source_not_unique")
    if str(row[6] or "") != item["identity_hmac"]:
        raise ValueError("source_changed")
    values = _json(row[2])
    identity = normalize_identity(values.get("身份证号"))
    expected = normalize_qmf_status_result(values.get("核查结果"))
    if not valid_identity(identity):
        raise ValueError("identity_invalid")
    if not expected:
        raise ValueError("result_invalid")
    if expected != normalize_qmf_status_result(item["expected_result"]):
        raise ValueError("source_changed")
    return identity, expected


async def _origin_for_item(item: dict[str, Any], state: str) -> str:
    if state not in {STATUS_COMPLETED_MATCH, STATUS_COMPLETED_MISMATCH}:
        return ""
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM _qmf_registration_runs WHERE parser_type=%s "
                "AND source_id=%s AND status='succeeded' LIMIT 1",
                (item["parser_type"], item["source_id"]),
            )
            return "binhu_automatic" if await cur.fetchone() else "legacy_manual_or_other"


async def persist_realtime_qmf_status(
    conn,
    *,
    parser_type: str,
    row_key: str,
    source_id: int,
    source_revision: int,
    source_row_hash: str,
    platform_result: str,
    status: QmfLegacyStatus,
) -> None:
    """Refresh the task snapshot after a successful single-item live check.

    The live status endpoint is not a scan batch, so it uses ``scan_run_id=0``
    while retaining the same privacy-safe snapshot shape.  Unavailable,
    ambiguous, and other unsafe responses are deliberately not written over a
    previous cache value; the caller still displays the live error separately.
    """
    if parser_type != MODEL_THREE_PARSER:
        return
    # Route-level tests and lightweight callers may not provide a database
    # connection.  The live response is still returned to the caller; only
    # the optional cache refresh is skipped in that case.
    if conn is None or not hasattr(conn, "cursor"):
        return
    accepted_states = {
        STATUS_COMPLETED_MATCH,
        STATUS_COMPLETED_MISMATCH,
        STATUS_NON_JURISDICTION,
        "pending",
        "not_found",
    }
    if status.state not in accepted_states:
        return
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(identity_hmac, '') FROM _online_source_projection "
            "WHERE parser_type=%s AND row_key=%s LIMIT 1",
            (parser_type, row_key),
        )
        identity_row = await cur.fetchone()
        identity_hmac = str(identity_row[0] or "") if identity_row else ""
        origin = (
            status.origin
            if status.state in {STATUS_COMPLETED_MATCH, STATUS_COMPLETED_MISMATCH}
            else ""
        )
        await cur.execute(
            """
            INSERT INTO _qmf_status_snapshots (
                parser_type,row_key,source_id,source_revision,
                source_row_hash,identity_hmac,platform_result,
                feedback_state,feedback_result,checked_at,origin,
                error_code,scan_run_id,matched_at,archive_due_at,archived_at,last_scanned_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'',0,
                IF(%s='completed_match',UTC_TIMESTAMP(),NULL),
                IF(%s='completed_match',DATE_SUB(DATE_ADD(DATE(DATE_ADD(UTC_TIMESTAMP(),INTERVAL 8 HOUR)),INTERVAL 1 DAY),INTERVAL 8 HOUR),NULL),NULL,UTC_TIMESTAMP())
            ON DUPLICATE KEY UPDATE
                source_id=VALUES(source_id),
                source_revision=VALUES(source_revision),
                source_row_hash=VALUES(source_row_hash),
                identity_hmac=VALUES(identity_hmac),
                platform_result=VALUES(platform_result),
                feedback_state=VALUES(feedback_state),
                feedback_result=VALUES(feedback_result),
                checked_at=VALUES(checked_at),
                origin=VALUES(origin),error_code='',
                matched_at=IF(VALUES(feedback_state)='completed_match',COALESCE(matched_at,VALUES(matched_at)),NULL),
                archive_due_at=IF(VALUES(feedback_state)='completed_match',COALESCE(archive_due_at,VALUES(archive_due_at)),NULL),
                archived_at=IF(VALUES(feedback_state)='completed_match',archived_at,NULL),
                scan_run_id=0,last_scanned_at=UTC_TIMESTAMP()
            """,
            (
                parser_type,
                row_key,
                int(source_id),
                int(source_revision),
                str(source_row_hash or "")[:64],
                identity_hmac[:64],
                normalize_qmf_status_result(platform_result),
                status.state,
                status.result,
                status.checked_at,
                origin,
                status.state,
                status.state,
            ),
        )


async def _finish_item(
    run_id: int,
    item: dict[str, Any],
    *,
    status: QmfLegacyStatus | None = None,
    error_code: str = "",
    duration_ms: int = 0,
) -> None:
    state = status.state if status else "unavailable"
    feedback_result = status.result if status else ""
    checked_at = status.checked_at if status else ""
    origin = await _origin_for_item(item, state)
    accepted_states = {
        STATUS_COMPLETED_MATCH,
        STATUS_COMPLETED_MISMATCH,
        STATUS_NON_JURISDICTION,
        "pending",
        "not_found",
    }
    is_error = bool(error_code) or state not in accepted_states
    bucket = _state_bucket(state if not is_error else "unavailable")
    final_status = "failed" if is_error else "completed"
    safe_code = (error_code or (state if is_error else ""))[:64]
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE _qmf_status_scan_items
                SET status=%s,feedback_state=%s,feedback_result=%s,
                    checked_at=%s,origin=%s,error_code=%s,
                    duration_ms=%s,finished_at=UTC_TIMESTAMP()
                WHERE id=%s AND run_id=%s
                """,
                (
                    final_status,
                    state,
                    feedback_result,
                    checked_at,
                    origin,
                    safe_code,
                    max(0, duration_ms),
                    item["id"],
                    run_id,
                ),
            )
            await cur.execute(
                """
                INSERT INTO _qmf_status_snapshots (
                    parser_type,row_key,source_id,source_revision,
                    source_row_hash,identity_hmac,platform_result,
                    feedback_state,feedback_result,checked_at,origin,
                    error_code,scan_run_id,matched_at,archive_due_at,archived_at,last_scanned_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    IF(%s='completed_match',UTC_TIMESTAMP(),NULL),
                    IF(%s='completed_match',DATE_SUB(DATE_ADD(DATE(DATE_ADD(UTC_TIMESTAMP(),INTERVAL 8 HOUR)),INTERVAL 1 DAY),INTERVAL 8 HOUR),NULL),NULL,UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE
                    source_id=VALUES(source_id),
                    source_revision=VALUES(source_revision),
                    source_row_hash=VALUES(source_row_hash),
                    identity_hmac=VALUES(identity_hmac),
                    platform_result=VALUES(platform_result),
                    feedback_state=VALUES(feedback_state),
                    feedback_result=VALUES(feedback_result),
                    checked_at=VALUES(checked_at),
                    origin=VALUES(origin),error_code=VALUES(error_code),
                    matched_at=IF(VALUES(feedback_state)='completed_match',COALESCE(matched_at,VALUES(matched_at)),NULL),
                    archive_due_at=IF(VALUES(feedback_state)='completed_match',COALESCE(archive_due_at,VALUES(archive_due_at)),NULL),
                    archived_at=IF(VALUES(feedback_state)='completed_match',archived_at,NULL),
                    scan_run_id=VALUES(scan_run_id),last_scanned_at=UTC_TIMESTAMP()
                """,
                (
                    item["parser_type"], item["row_key"], item["source_id"],
                    item["expected_revision"], item["expected_row_hash"],
                    item["identity_hmac"],
                    normalize_qmf_status_result(item["expected_result"]), state,
                    feedback_result, checked_at, origin, safe_code, run_id,
                    state, state,
                ),
            )
            await cur.execute(
                f"UPDATE _qmf_status_scan_runs SET processed_count=processed_count+1, "
                f"{bucket}={bucket}+1 WHERE id=%s",
                (run_id,),
            )


async def _finish_run(run_id: int, *, stopped_code: str = "") -> None:
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT total_count,processed_count,error_count FROM "
                "_qmf_status_scan_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
            if not row:
                return
            total, processed, errors = map(int, row)
            status = (
                "failed" if processed == 0 and stopped_code
                else "partial" if processed < total or errors
                else "completed"
            )
            await cur.execute(
                "UPDATE _qmf_status_scan_runs SET status=%s,error_code=%s,"
                "finished_at=UTC_TIMESTAMP() WHERE id=%s",
                (status, stopped_code[:64], run_id),
            )


async def run_status_scan(run_id: int) -> None:
    stop = asyncio.Event()
    failure_lock = asyncio.Lock()
    consecutive_failures = 0
    stop_code = ""
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _qmf_status_scan_runs SET status='running',"
                "started_at=COALESCE(started_at,UTC_TIMESTAMP()),error_code='' "
                "WHERE id=%s AND status IN ('queued','running')",
                (run_id,),
            )
    client = QmfLegacyStatusClient()
    try:
        async with client.session() as session:
            async def worker() -> None:
                nonlocal consecutive_failures, stop_code
                while not stop.is_set():
                    item = await _claim_item(run_id)
                    if item is None:
                        return
                    started = time.monotonic()
                    try:
                        identity, expected = await _current_item_context(item)
                        result = await session.query(
                            identity=identity,
                            expected_result=expected,
                        )
                        await _finish_item(
                            run_id,
                            item,
                            status=result,
                            duration_ms=int((time.monotonic() - started) * 1000),
                        )
                        async with failure_lock:
                            if result.state == STATUS_UNAVAILABLE:
                                consecutive_failures += 1
                            else:
                                consecutive_failures = 0
                            if consecutive_failures >= FAILURE_CIRCUIT_LIMIT:
                                stop_code = "consecutive_unavailable"
                                stop.set()
                    except QmfStatusAccessError as exc:
                        await _finish_item(
                            run_id,
                            item,
                            error_code=exc.code,
                            duration_ms=int((time.monotonic() - started) * 1000),
                        )
                        stop_code = exc.code
                        stop.set()
                    except ValueError as exc:
                        await _finish_item(
                            run_id,
                            item,
                            error_code=str(exc)[:64],
                            duration_ms=int((time.monotonic() - started) * 1000),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await _finish_item(
                            run_id,
                            item,
                            error_code="unexpected_error",
                            duration_ms=int((time.monotonic() - started) * 1000),
                        )

            await asyncio.gather(*(worker() for _ in range(SCAN_CONCURRENCY)))
    except QmfStatusAccessError as exc:
        stop_code = exc.code
    except asyncio.CancelledError:
        raise
    except Exception:
        stop_code = "scan_session_failed"
    await _finish_run(run_id, stopped_code=stop_code)
    try:
        archived = await archive_due_qmf_tasks()
        if archived:
            print(f"[QMF_STATUS_SCAN] 已归档全民防反馈一致且满一天任务 {archived} 条")
    except Exception as exc:  # 归档失败不覆盖扫描结果，下次扫描继续重试
        print(f"[QMF_STATUS_SCAN] 延迟归档失败：{type(exc).__name__}")


def launch_status_scan(run_id: int) -> None:
    if any(
        not task.done() and getattr(task, "qmf_scan_run_id", None) == run_id
        for task in _background_tasks
    ):
        return
    task = asyncio.create_task(run_status_scan(run_id))
    setattr(task, "qmf_scan_run_id", run_id)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def recover_status_scans() -> int:
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _qmf_status_scan_items SET status='pending',"
                "started_at=NULL WHERE status='processing'"
            )
            await cur.execute(
                "SELECT id FROM _qmf_status_scan_runs "
                "WHERE status IN ('queued','running') ORDER BY id LIMIT 1"
            )
            row = await cur.fetchone()
    if row:
        launch_status_scan(int(row[0]))
        return 1
    return 0


async def stop_status_scan_tasks() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def maybe_launch_scheduled_scan(now: datetime | None = None) -> int | None:
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    pool = _pool()
    async with pool.acquire() as conn:
        config = await load_qmf_config(conn)
    if not config.status_scan_enabled or not valid_schedule_time(config.status_scan_time):
        return None
    if current.strftime("%H:%M") < config.status_scan_time:
        return None
    scheduled_date = current.date()
    try:
        run_id, _ = await create_status_scan_run(
            trigger_source="scheduled",
            requested_by=None,
            scheduled_date=scheduled_date,
        )
        return run_id
    except RuntimeError as exc:
        if str(exc) in {"scan_busy", "scan_already_scheduled"}:
            return None
        raise
    except Exception as exc:
        if "uk_qmf_status_scan_schedule" in str(exc):
            return None
        raise


async def run_status_scan_scheduler() -> None:
    while True:
        try:
            await maybe_launch_scheduled_scan()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[QMF_STATUS_SCAN] scheduler failed: {type(exc).__name__}")
        await asyncio.sleep(30)


async def status_scan_payload(run_id: int) -> dict[str, Any] | None:
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id,trigger_source,scan_mode,status,concurrency,total_count,
                       processed_count,match_count,mismatch_count,pending_count,
                       not_found_count,non_jurisdiction_count,error_count,requested_by,error_code,
                       started_at,finished_at,created_at,updated_at
                FROM _qmf_status_scan_runs WHERE id=%s
                """,
                (run_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            await cur.execute(
                "SELECT error_code,COUNT(*) FROM _qmf_status_scan_items "
                "WHERE run_id=%s AND error_code<>'' GROUP BY error_code "
                "ORDER BY COUNT(*) DESC,error_code LIMIT 20",
                (run_id,),
            )
            failures = [
                {"code": str(code or "unknown"), "count": int(count or 0)}
                for code, count in await cur.fetchall()
            ]
    return {
        "id": int(row[0]),
        "trigger_source": str(row[1]),
        "scan_mode": str(row[2]),
        "status": str(row[3]),
        "concurrency": int(row[4]),
        "total_count": int(row[5]),
        "processed_count": int(row[6]),
        "match_count": int(row[7]),
        "mismatch_count": int(row[8]),
        "pending_count": int(row[9]),
        "not_found_count": int(row[10]),
        "non_jurisdiction_count": int(row[11]),
        "error_count": int(row[12]),
        "requested_by": int(row[13]) if row[13] is not None else None,
        "error_code": str(row[14] or ""),
        "started_at": _utc_text(row[15]),
        "finished_at": _utc_text(row[16]),
        "created_at": _utc_text(row[17]),
        "updated_at": _utc_text(row[18]),
        "failures": failures,
    }


async def latest_status_scan_payload() -> dict[str, Any] | None:
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM _qmf_status_scan_runs ORDER BY id DESC LIMIT 1")
            row = await cur.fetchone()
    return await status_scan_payload(int(row[0])) if row else None
