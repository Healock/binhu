"""Durable, metadata-only triggers for immediate online-summary refreshes.

The task tables remain the only source of truth.  A successful task mutation
records one small trigger in the same OnlineData transaction; a bounded
consumer can then read the current task row and update the daily-report
projection.  The trigger deliberately contains no task body or person data.
"""

from __future__ import annotations

from datetime import date
import re
import asyncio
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from config import settings


_PARSER_TYPE_LIMIT = 50
_ROW_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class UnsupportedSummaryParser(ValueError):
    """A task type which is intentionally outside online-summary reports."""


class StaleSummaryTrigger(ValueError):
    """The trigger was superseded by a newer authoritative source revision."""


async def ensure_online_summary_update_schema(cur) -> None:
    """Create the local trigger table if it is missing.

    This table belongs to OnlineData and is initialized alongside the other
    local projection queues.  It is intentionally only a trigger ledger: the
    consumer must re-read the authoritative task row by ``task_id``.
    """
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _online_summary_updates (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            task_id BIGINT UNSIGNED NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            row_key VARCHAR(128) NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            business_date DATE NOT NULL,
            operation_id VARCHAR(128) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
            next_attempt_at DATETIME DEFAULT NULL,
            error_code VARCHAR(80) NOT NULL DEFAULT '',
            last_error VARCHAR(500) NOT NULL DEFAULT '',
            finished_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_online_summary_update
                (parser_type, task_id, revision, business_date),
            INDEX idx_online_summary_update_due
                (status, next_attempt_at, created_at),
            INDEX idx_online_summary_update_task
                (task_id, business_date, revision)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )
    # Existing installations may already have the trigger table from an
    # earlier build.  Keep the migration additive and idempotent.
    await cur.execute(
        "SHOW COLUMNS FROM _online_summary_updates LIKE 'finished_at'"
    )
    if not await cur.fetchone():
        await cur.execute(
            "ALTER TABLE _online_summary_updates "
            "ADD COLUMN finished_at DATETIME DEFAULT NULL AFTER last_error"
        )


def _validate_metadata(
    *,
    task_id: int,
    parser_type: str,
    row_key: str,
    revision: int,
    business_date: date,
    operation_id: str,
) -> tuple[int, str, str, int, date, str]:
    try:
        normalized_task_id = int(task_id)
        normalized_revision = int(revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("汇总更新必须包含有效 task_id 和 revision") from exc
    if normalized_task_id <= 0 or normalized_revision <= 0:
        raise ValueError("汇总更新必须包含正数 task_id 和 revision")
    normalized_parser = str(parser_type or "").strip()
    if not normalized_parser or len(normalized_parser) > _PARSER_TYPE_LIMIT:
        raise ValueError("汇总更新的 parser_type 无效")
    normalized_row_key = str(row_key or "").strip()
    if not _ROW_KEY_RE.fullmatch(normalized_row_key):
        raise ValueError("汇总更新的 row_key 无效")
    if not isinstance(business_date, date):
        raise ValueError("汇总更新必须包含 business_date")
    normalized_operation = str(operation_id or "").strip()
    if not _OPERATION_ID_RE.fullmatch(normalized_operation):
        raise ValueError("汇总更新的 operation_id 无效")
    return (
        normalized_task_id,
        normalized_parser,
        normalized_row_key,
        normalized_revision,
        business_date,
        normalized_operation,
    )


async def enqueue_online_summary_update(
    cur,
    *,
    task_id: int,
    parser_type: str,
    row_key: str,
    revision: int,
    business_date: date,
    operation_id: str,
) -> bool:
    """Add one idempotent post-commit summary trigger.

    ``INSERT`` is part of the caller's business transaction.  A duplicate
    ``task_id + revision + business_date`` is a no-op, which makes retries and
    repeated callbacks safe.  No task values are copied into this table.
    """
    values = _validate_metadata(
        task_id=task_id,
        parser_type=parser_type,
        row_key=row_key,
        revision=revision,
        business_date=business_date,
        operation_id=operation_id,
    )
    await cur.execute(
        """
        INSERT INTO _online_summary_updates (
            task_id, parser_type, row_key, revision, business_date, operation_id,
            status
        ) VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        ON DUPLICATE KEY UPDATE
            operation_id=VALUES(operation_id),
            updated_at=CURRENT_TIMESTAMP
        """,
        values,
    )
    return True


def should_apply_revision(current_revision: int | None, incoming_revision: int) -> bool:
    """Return whether a trigger may advance a task's summary state."""
    if current_revision is None:
        return True
    return int(incoming_revision) > int(current_revision)


def update_trigger_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Expose only safe trigger metadata to diagnostics and metrics."""
    return {
        "id": int(row["id"]),
        "task_id": int(row["task_id"]),
        "parser_type": str(row["parser_type"]),
        "revision": int(row["revision"]),
        "business_date": row["business_date"].isoformat(),
        "status": str(row["status"]),
        "attempt_count": int(row.get("attempt_count") or 0),
    }


def effective_workload_transition(previous_state: str | None, current_state: str) -> int:
    """Mirror BaseReportBuilder.ledger_effective_workload_sql exactly."""
    if previous_state is None:
        return int(current_state != "unchecked")
    return int(
        (previous_state == "unchecked" and current_state in {"checked", "completed"})
        or (previous_state == "checked" and current_state == "completed")
    )


def effective_workload_for_update(
    previous_workload: int,
    historical_workload: int,
    transition: int,
) -> int:
    """Return this task's single-day credit, capped at one row transition.

    ``effective_workload`` is a per-task/per-day value.  The historical
    two-day cap is applied in addition to the per-day cap; otherwise two
    same-day revisions could accidentally write ``2`` into one ledger row.
    """
    return min(
        1,
        max(0, int(previous_workload)) + max(0, int(transition)),
        max(0, 2 - max(0, int(historical_workload))),
    )


def _same_authoritative_revision(
    expected: dict[str, Any] | None,
    latest: dict[str, Any] | None,
) -> bool:
    """Compare the identity and revision used by the final commit fence."""
    if expected is None or latest is None:
        return expected is None and latest is None
    return (
        int(expected.get("task_id") or 0) == int(latest.get("task_id") or 0)
        and int(expected.get("revision") or 0) == int(latest.get("revision") or 0)
    )


async def _assert_authoritative_fence(
    row: dict[str, Any], expected: dict[str, Any] | None
) -> None:
    """Re-read the source immediately before committing the daily ledger."""
    latest = await _load_authoritative_task(row)
    if not _same_authoritative_revision(expected, latest):
        if latest is None or expected is None:
            raise StaleSummaryTrigger("authoritative_revision_changed")
        if int(latest["revision"]) > int(expected["revision"]):
            raise StaleSummaryTrigger("superseded_by_newer_revision")
        raise ValueError("authoritative_revision_changed")


def _summary_pool():
    from database import db_manager

    return db_manager.get_pool("online_data")


def _daily_pool():
    from database import db_manager

    return db_manager.get_pool("daily_report")


def _online_table(table: str) -> str:
    """Qualify an OnlineData table using the active environment database."""
    database = str(settings.MYSQL_ONLINE_DATA_DB).replace("`", "``")
    identifier = str(table).replace("`", "``")
    return f"`{database}`.`{identifier}`"


def _organization_table(table: str) -> str:
    """Qualify personnel/community tables in the active organization domain."""
    database_name = (
        settings.MYSQL_PLATFORM_DB
        if bool(getattr(settings, "PLATFORM_DOMAIN_ACTIVE", False))
        else settings.MYSQL_ONLINE_DATA_DB
    )
    database = str(database_name).replace("`", "``")
    identifier = str(table).replace("`", "``")
    return f"`{database}`.`{identifier}`"


async def _acquire(pool):
    """Acquire with the same bounded timeout used by request dependencies."""
    return await asyncio.wait_for(
        pool.acquire(), timeout=settings.MYSQL_POOL_ACQUIRE_TIMEOUT_SECONDS
    )


async def _claim_update_rows(limit: int) -> list[dict[str, Any]]:
    pool = _summary_pool()
    conn = await _acquire(pool)
    try:
        async with conn.cursor() as cur:
            await conn.begin()
            try:
                await cur.execute(
                    "SELECT id,task_id,parser_type,row_key,revision,business_date,attempt_count "
                    "FROM _online_summary_updates WHERE status IN ('pending','retry') "
                    "AND (next_attempt_at IS NULL OR next_attempt_at<=UTC_TIMESTAMP()) "
                    "ORDER BY created_at,id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (max(1, min(int(limit), 50)),),
                )
                rows = await cur.fetchall()
                if rows:
                    ids = [int(row[0]) for row in rows]
                    marks = ",".join(["%s"] * len(ids))
                    await cur.execute(
                        f"UPDATE _online_summary_updates SET status='running',attempt_count=attempt_count+1,error_code='' WHERE id IN ({marks})",
                        ids,
                    )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    finally:
        pool.release(conn)
    return [
        {"id": int(r[0]), "task_id": int(r[1]), "parser_type": str(r[2]),
         "row_key": str(r[3]), "revision": int(r[4]), "business_date": r[5],
         "attempt_count": int(r[6]) + 1}
        for r in rows
    ]


async def _finish_update(row_id: int, status: str, error_code: str = "") -> None:
    pool = _summary_pool()
    conn = await _acquire(pool)
    try:
        async with conn.cursor() as cur:
            if status == "retry":
                await cur.execute(
                    "UPDATE _online_summary_updates SET status='retry',error_code=%s,next_attempt_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL 2 SECOND) WHERE id=%s",
                    (error_code, row_id),
                )
            else:
                await cur.execute(
                    "UPDATE _online_summary_updates SET status=%s,error_code=%s,finished_at=UTC_TIMESTAMP(),next_attempt_at=NULL WHERE id=%s",
                    (status, error_code, row_id),
                )
            await conn.commit()
    finally:
        pool.release(conn)


async def _load_authoritative_task(row: dict[str, Any]) -> dict[str, Any] | None:
    from services.report_builders import BUILDERS
    from services.parsers import get_parser

    builder = BUILDERS.get(row["parser_type"])
    if builder is None:
        raise UnsupportedSummaryParser("unsupported_parser_type")
    parser = get_parser(row["parser_type"])
    table = parser.table_name.replace("`", "")
    columns = ["_row_key", "id", "_last_updated_at", builder.community_column, builder.inspector_column]
    result_column = builder.result_column
    if result_column in parser.COLUMNS:
        columns.append(result_column)
    if "现住址" in parser.COLUMNS:
        columns.append("现住址")
    selected = ",".join(f"t.`{column}`" for column in dict.fromkeys(columns))
    state_sql = builder.ledger_state_sql("t")
    unable_sql = builder.ledger_unable_sql("t")
    reached_sql = builder.ledger_reached_bottom_sql("t")
    pool = _summary_pool()
    conn = await _acquire(pool)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT t.`_row_key`,t.`id`,"
                "COALESCE(NULLIF(TRIM(responsibility.first_community), ''), '未分配社区'),"
                "TRIM(IFNULL(responsibility.first_inspector, '')),"
                f"{state_sql},{unable_sql},{reached_sql},s.revision "
                f"FROM `{table}` t JOIN _online_source_rows s ON s.parser_type=%s AND s.row_key=t.`_row_key` AND s.archived_at IS NULL "
                f"LEFT JOIN {_online_table('_task_assignment_responsibilities')} responsibility "
                "ON responsibility.parser_type=%s AND responsibility.row_key=t.`_row_key` "
                "WHERE t.`_row_key`=%s ORDER BY s.revision DESC LIMIT 1",
                (row["parser_type"], row["parser_type"], row["row_key"]),
            )
            current = await cur.fetchone()
    finally:
        pool.release(conn)
    if not current:
        return None
    return {
        "row_key": str(current[0]), "task_id": int(current[1]),
        "community": str(current[2] or ""), "inspector": str(current[3] or ""),
        "task_state": str(current[4]), "unable_to_verify": int(current[5] or 0),
        "reached_bottom": int(current[6] or 0), "revision": int(current[7] or 0),
    }


async def _ensure_incremental_report_tables(
    parser_type: str,
    report_date,
) -> None:
    """Create/register report tables before the ledger transaction.

    MySQL DDL implicitly commits, so these operations must not run while the
    daily ledger transaction is open.  Names are derived from the registered
    builder and date rather than caller-provided SQL identifiers.
    """
    from services.report_builders import BUILDERS

    builder = BUILDERS.get(parser_type)
    if builder is None:
        raise UnsupportedSummaryParser("unsupported_parser_type")
    date_text = (
        report_date.isoformat()
        if hasattr(report_date, "isoformat")
        else str(report_date)
    )
    inspector_name = f"{date_text}_daily_{builder.table_suffix}_inspector"
    community_name = f"{date_text}_daily_{builder.table_suffix}_community"
    inspector_table = f"`{inspector_name.replace('`', '``')}`"
    community_table = f"`{community_name.replace('`', '``')}`"

    pool = _daily_pool()
    conn = await _acquire(pool)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "CREATE TABLE IF NOT EXISTS _daily_report_meta ("
                "id INT AUTO_INCREMENT PRIMARY KEY,"
                "table_name VARCHAR(100) NOT NULL,"
                "report_date DATE NOT NULL,"
                "parser_type VARCHAR(50) NOT NULL,"
                "generation_method VARCHAR(20) DEFAULT 'auto',"
                "generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
                "UNIQUE KEY uk_table_name (table_name),"
                "INDEX idx_date (report_date),"
                "INDEX idx_type (parser_type)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
                "COLLATE=utf8mb4_unicode_ci"
            )
            await cur.execute(
                f"CREATE TABLE IF NOT EXISTS {inspector_table} "
                f"({builder.INSPECTOR_COLS}) ENGINE=InnoDB "
                "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
            )
            await cur.execute(
                f"CREATE TABLE IF NOT EXISTS {community_table} "
                f"({builder.COMMUNITY_COLS}) ENGINE=InnoDB "
                "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
            )
            for table_name in (inspector_name, community_name):
                await cur.execute(
                    "INSERT INTO _daily_report_meta "
                    "(table_name, report_date, parser_type, generation_method) "
                    "VALUES (%s, %s, %s, 'incremental') "
                    "ON DUPLICATE KEY UPDATE generation_method='incremental', "
                    "generated_at=UTC_TIMESTAMP()",
                    (table_name, report_date, parser_type),
                )
            await conn.commit()
    except BaseException:
        with suppress(Exception):
            await conn.rollback()
        raise
    finally:
        pool.release(conn)


async def _apply_update(row: dict[str, Any]) -> None:
    current = await _load_authoritative_task(row)
    if current is not None:
        if int(current["task_id"]) != int(row["task_id"]):
            raise StaleSummaryTrigger("superseded_by_task_identity")
        current_revision = int(current["revision"])
        if current_revision > int(row["revision"]):
            raise StaleSummaryTrigger("superseded_by_newer_revision")
        if current_revision < int(row["revision"]):
            raise ValueError("authoritative_revision_not_visible")
    await _ensure_incremental_report_tables(
        row["parser_type"], row["business_date"]
    )
    pool = _daily_pool()
    conn = await _acquire(pool)
    try:
        async with conn.cursor() as cur:
            await conn.begin()
            try:
                await cur.execute(
                    "SELECT task_state,effective_workload,source_revision,community,inspector,"
                    "included,unable_to_verify,reached_bottom "
                    "FROM _daily_task_ledger WHERE report_date=%s AND parser_type=%s AND row_key=%s FOR UPDATE",
                    (row["business_date"], row["parser_type"], row["row_key"]),
                )
                previous = await cur.fetchone()
                if previous and int(previous[2] or 0) >= int(row["revision"]):
                    await conn.commit()
                    return
                previous_state = str(previous[0]) if previous else None
                previous_workload = int(previous[1] or 0) if previous else 0
                historical_previous = None
                if previous is None:
                    # A trigger may be the first event for a new business
                    # date.  Use the last historical ledger state, rather
                    # than treating an already completed task as newly
                    # completed again.
                    await cur.execute(
                        "SELECT task_state,effective_workload,community,inspector "
                        "FROM _daily_task_ledger "
                        "WHERE report_date < %s "
                        "AND parser_type=%s AND row_key=%s "
                        "ORDER BY report_date DESC LIMIT 1",
                        (row["business_date"], row["parser_type"], row["row_key"]),
                    )
                    historical_state = await cur.fetchone()
                    if historical_state:
                        historical_previous = historical_state
                        previous_state = str(historical_state[0] or "")
                        # The historical row supplies the comparison state;
                        # its workload belongs to the previous date and must
                        # not be copied into today's per-day ledger row.
                        previous_workload = 0
                if current is None:
                    if previous_state is None:
                        await conn.commit()
                        return
                    baseline_community = (
                        previous[3] if previous is not None else historical_previous[2]
                    )
                    baseline_inspector = (
                        previous[4] if previous is not None else historical_previous[3]
                    )
                    values = {
                        # Preserve completed work credited earlier today,
                        # matching the regular daily-ledger archive rule.
                        "source": "removed",
                        "included": int(previous[5]) if previous and previous_state == "completed" else 0,
                        "online_present": 0,
                        "community": baseline_community or "",
                        "inspector": baseline_inspector or "",
                        "task_state": previous_state,
                        "unable_to_verify": int(previous[6]) if previous else 0,
                        "reached_bottom": int(previous[7]) if previous else 0,
                        # Removing a task from the online snapshot must not
                        # erase work already credited on that date.
                        "effective_workload": previous_workload,
                    }
                else:
                    await cur.execute(
                        "SELECT COALESCE(SUM(effective_workload), 0) "
                        "FROM _daily_task_ledger WHERE report_date < %s "
                        "AND parser_type=%s AND row_key=%s",
                        (row["business_date"], row["parser_type"], row["row_key"]),
                    )
                    historical_workload = int((await cur.fetchone())[0] or 0)
                    transition = effective_workload_transition(
                        previous_state, current["task_state"]
                    )
                    values = {
                        "source": "activity", "included": 1,
                        "online_present": 1, "community": current["community"],
                        "inspector": current["inspector"],
                        "task_state": current["task_state"],
                        "unable_to_verify": current["unable_to_verify"],
                        "reached_bottom": current["reached_bottom"],
                        "effective_workload": effective_workload_for_update(
                            previous_workload, historical_workload, transition
                        ),
                    }
                await _assert_authoritative_fence(row, current)
                await cur.execute(
                    "INSERT INTO _daily_task_ledger (report_date,parser_type,row_key,source,included,online_present,community,inspector,task_state,unable_to_verify,reached_bottom,effective_workload,source_revision) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE source=VALUES(source),included=VALUES(included),online_present=VALUES(online_present),community=VALUES(community),inspector=VALUES(inspector),task_state=VALUES(task_state),unable_to_verify=VALUES(unable_to_verify),reached_bottom=VALUES(reached_bottom),effective_workload=VALUES(effective_workload),source_revision=VALUES(source_revision),updated_at=CURRENT_TIMESTAMP",
                    (row["business_date"], row["parser_type"], row["row_key"], values["source"], values["included"], values["online_present"], values["community"], values["inspector"], values["task_state"], values["unable_to_verify"], values["reached_bottom"], values["effective_workload"], row["revision"]),
                )
                await _refresh_affected_report_groups(
                    cur,
                    parser_type=row["parser_type"],
                    report_date=row["business_date"],
                    row_key=row["row_key"],
                    previous_community=str(previous[3] or "") if previous else "",
                    current_community=str(values["community"] or ""),
                )
                # The source and daily-report databases are separate
                # transactions.  Re-check immediately before the daily
                # commit so a save which landed during aggregation cannot be
                # mistaken for this trigger's revision.
                await _assert_authoritative_fence(row, current)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    finally:
        pool.release(conn)


async def _refresh_affected_report_groups(
    cur,
    *,
    parser_type: str,
    report_date,
    row_key: str,
    previous_community: str,
    current_community: str,
) -> None:
    """Refresh only inspector rows touched by one task mutation.

    The public online-summary reader consumes these inspector tables and
    performs its existing cross-builder merge.  Rebuilding the affected
    groups from the durable ledger preserves that behavior while avoiding a
    full daily snapshot or full report rebuild on every save.
    """
    from services.report_builders import BUILDERS
    from services.report_members import insert_zero_member_rows

    builder = BUILDERS.get(parser_type)
    if builder is None:
        raise ValueError("unsupported_parser_type")
    date_text = report_date.isoformat() if hasattr(report_date, "isoformat") else str(report_date)
    inspector_table = f"`{date_text}_daily_{builder.table_suffix}_inspector`"
    community_table = f"`{date_text}_daily_{builder.table_suffix}_community`"
    await cur.execute(
        "SELECT DISTINCT COALESCE(formal.name, ledger.community) "
        "FROM _daily_task_ledger ledger "
        f"LEFT JOIN {_organization_table('_community_aliases')} alias ON alias.alias=ledger.community "
        f"LEFT JOIN {_organization_table('_communities')} formal ON formal.id=alias.community_id "
        "WHERE ledger.report_date=%s AND ledger.parser_type=%s "
        "AND ledger.row_key=%s",
        (report_date, parser_type, row_key),
    )
    groups = {str(item[0] or "未分配社区") for item in await cur.fetchall()}
    if previous_community:
        await cur.execute(
            f"SELECT COALESCE(formal.name, %s) FROM {_organization_table('_community_aliases')} alias "
            f"LEFT JOIN {_organization_table('_communities')} formal ON formal.id=alias.community_id "
            "WHERE alias.alias=%s LIMIT 1",
            (previous_community, previous_community),
        )
        old = await cur.fetchone()
        groups.add(str(old[0] if old and old[0] else previous_community))
    if not groups:
        return
    marks = ",".join(["%s"] * len(groups))
    params = [*sorted(groups)]
    # Serialize all ledger rows in the affected communities before replacing
    # their materialized inspector rows.  This prevents two tasks in one
    # community from deleting each other's freshly aggregated result.
    await cur.execute(
        f"SELECT ledger.row_key FROM _daily_task_ledger ledger "
        f"LEFT JOIN {_organization_table('_community_aliases')} lock_alias "
        "ON lock_alias.alias=ledger.community "
        f"LEFT JOIN {_organization_table('_communities')} lock_formal "
        "ON lock_formal.id=lock_alias.community_id "
        f"WHERE ledger.report_date=%s AND ledger.parser_type=%s "
        f"AND COALESCE(lock_formal.name, ledger.community) IN ({marks}) "
        "ORDER BY ledger.row_key "
        "FOR UPDATE",
        [report_date, parser_type, *sorted(groups)],
    )
    await cur.execute(
        f"DELETE FROM {inspector_table} WHERE 社区 IN ({marks})",
        params,
    )
    state = "ledger.task_state"
    await cur.execute(
        f"""
        INSERT INTO {inspector_table}
            (社区,姓名,数据总数,未核查,已核查,已完成,核查完成率,无法见底数,核查见底率)
        SELECT COALESCE(formal.name, ledger.community), ledger.inspector,
               COUNT(*), SUM({state}='unchecked'), SUM({state}='checked'),
               SUM({state}='completed'),
               ROUND(SUM({state}='completed')/COUNT(*),2),
               SUM(ledger.unable_to_verify),
               CASE WHEN SUM({state}='completed')+SUM(ledger.unable_to_verify)>0
                    THEN ROUND(SUM({state}='completed')/
                        (SUM({state}='completed')+SUM(ledger.unable_to_verify)),2)
                    ELSE 0 END
        FROM _daily_task_ledger ledger
        LEFT JOIN {_organization_table('_community_aliases')} alias ON alias.alias=ledger.community
        LEFT JOIN {_organization_table('_communities')} formal ON formal.id=alias.community_id
        WHERE ledger.report_date=%s AND ledger.parser_type=%s
          AND ledger.included=1 AND ledger.inspector<>''
          AND ledger.inspector<>'核查人' AND ledger.community<>''
          AND ledger.community NOT IN ('社区','下发社区')
          AND EXISTS (
            SELECT 1 FROM {_organization_table('_grid_members')} person
            JOIN {_organization_table('_grid_member_department_links')} person_link
              ON person_link.member_id=person.id
            JOIN {_organization_table('_departments')} department
              ON department.id=person_link.department_id
             AND department.department_type='community'
            JOIN {_organization_table('_communities')} person_community
              ON person_community.id=department.community_id
            WHERE LOWER(TRIM(person.name))=LOWER(TRIM(ledger.inspector))
              AND person.position IN ('组长','组员')
          )
          AND COALESCE(formal.name, ledger.community) IN ({marks})
        GROUP BY COALESCE(formal.name, ledger.community), ledger.inspector
        """,
        [report_date, parser_type, *sorted(groups)],
    )
    await insert_zero_member_rows(cur, inspector_table, date_text)
    # The tables are created and registered before the ledger transaction.
    # Keep this refresh limited to data changes so a revision-fence rollback
    # remains a real transaction rollback.
    await cur.execute(f"DELETE FROM {community_table} WHERE 社区 IN ({marks})", params)
    await cur.execute(
        f"""
        INSERT INTO {community_table}
            (社区,数据总数,未核查,已核查,已完成,核查完成率,无法见底数,核查见底率)
        SELECT 社区,SUM(数据总数),SUM(未核查),SUM(已核查),SUM(已完成),
               CASE WHEN SUM(数据总数)>0 THEN ROUND(SUM(已完成)/SUM(数据总数),2) ELSE 0 END,
               SUM(无法见底数),
               CASE WHEN SUM(已完成)+SUM(无法见底数)>0
                    THEN ROUND(SUM(已完成)/(SUM(已完成)+SUM(无法见底数)),2) ELSE 0 END
        FROM {inspector_table}
        WHERE 社区 IN ({marks})
        GROUP BY 社区
        """,
        params,
    )


async def _process_update(row: dict[str, Any]) -> None:
    try:
        await _apply_update(row)
        await _finish_update(row["id"], "succeeded")
    except UnsupportedSummaryParser:
        # Some task sources (for example police datasets) are deliberately
        # excluded from the online-summary builders.  They must not become a
        # permanent failed queue or hide a real processing error.
        await _finish_update(row["id"], "skipped", "unsupported_parser_type")
    except StaleSummaryTrigger as exc:
        # A newer trigger owns the authoritative state.  Mark this metadata
        # row complete without applying its superseded snapshot.
        await _finish_update(row["id"], "skipped", str(exc))
    except asyncio.CancelledError:
        await _finish_update(row["id"], "retry", "worker_cancelled")
        raise
    except Exception as exc:
        code = "summary_update_failed"
        if int(row["attempt_count"]) < 5:
            await _finish_update(row["id"], "retry", code)
        else:
            await _finish_update(row["id"], "failed", code)


async def process_online_summary_updates_once(limit: int = 10) -> int:
    """Apply a bounded batch; callers may run this after commit or in a loop."""
    await _ensure_day_baseline()
    async with _consumer_lock:
        rows = await _claim_update_rows(limit)
        if not rows:
            return 0
        semaphore = asyncio.Semaphore(
            max(1, min(int(os.environ.get("ONLINE_SUMMARY_WORKER_CONCURRENCY", "2")), 4))
        )

        async def bounded(item):
            async with semaphore:
                await _process_update(item)

        await asyncio.gather(*(bounded(item) for item in rows))
    return len(rows)


_baseline_date: date | None = None
_baseline_lock = asyncio.Lock()


async def _ensure_day_baseline() -> None:
    """Seed unchanged carryover tasks once per day before incremental updates.

    Isolated runners explicitly disable the report scheduler and seed their
    own fixtures. Production uses the existing local snapshot builder; a
    failed baseline keeps the durable queue pending instead of showing a
    partial day's population as a complete summary.
    """
    if not settings.LOCAL_REPORT_SCHEDULER_ENABLED:
        return
    from services.business_time import get_business_date
    global _baseline_date
    pool = _daily_pool()
    conn = await _acquire(pool)
    try:
        async with conn.cursor() as cur:
            today = await get_business_date(cur)
    finally:
        pool.release(conn)
    if _baseline_date == today:
        return
    async with _baseline_lock:
        if _baseline_date == today:
            return
        from services.local_report_scheduler import refresh_local_daily_reports_once
        result = await refresh_local_daily_reports_once()
        if result.get('status') != 'success':
            raise RuntimeError('summary_baseline_not_ready')
        _baseline_date = today


async def run_online_summary_update_worker() -> None:
    """Recover interrupted rows and consume triggers with bounded concurrency."""
    pool = _summary_pool()
    conn = await _acquire(pool)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _online_summary_updates SET status='retry',error_code='worker_recovered',next_attempt_at=NULL WHERE status='running'"
            )
        await conn.commit()
    finally:
        pool.release(conn)
    while True:
        try:
            processed = await process_online_summary_updates_once()
            await asyncio.sleep(0 if processed else 0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)


_wake_task: asyncio.Task | None = None
_consumer_lock = asyncio.Lock()


def launch_online_summary_update_processing() -> None:
    """Best-effort immediate wake-up; durable rows remain recoverable."""
    global _wake_task
    if _wake_task is None or _wake_task.done():
        _wake_task = asyncio.create_task(process_online_summary_updates_once(limit=10))


async def stop_online_summary_update_processing() -> None:
    """Cancel a post-commit wake task before database pools are closed."""
    global _wake_task
    task = _wake_task
    _wake_task = None
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
