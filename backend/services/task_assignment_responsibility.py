"""流口任务首次核查责任与所内移交安全记录。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import json
import re
from typing import Any

from services.task_workflow import TASK_WORKFLOWS


_SCHEMA_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _candidate(
    *,
    source: str,
    occurred_at: date | datetime | str,
    community: Any,
    inspector: Any,
) -> dict[str, Any] | None:
    normalized_community = _text(community)
    normalized_inspector = _text(inspector)
    if not normalized_community or not normalized_inspector:
        return None
    return {
        "source": source,
        "occurred_at": occurred_at,
        "community": normalized_community,
        "inspector": normalized_inspector,
    }


def resolve_first_assignment_candidate(
    assignment_events: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Resolve a historical first owner without using the current assignment.

    Explicit assignment/transfer events take precedence.  The daily ledger is
    only a fallback.  Conflicting candidates at the same earliest timestamp are
    returned as a problem instead of being guessed.
    """

    for candidates, source_label in (
        (assignment_events, "assignment_event"),
        (ledger_rows, "daily_ledger"),
    ):
        usable = [
            item for item in candidates
            if _text(item.get("community")) and _text(item.get("inspector"))
        ]
        if not usable:
            continue
        earliest = min(item.get("occurred_at") for item in usable)
        first = [item for item in usable if item.get("occurred_at") == earliest]
        owners = {
            (_text(item.get("community")), _text(item.get("inspector")))
            for item in first
        }
        if len(owners) != 1:
            return None, f"{source_label}_conflict"
        community, inspector = next(iter(owners))
        return {
            "community": community,
            "inspector": inspector,
            "capture_source": f"migration_{source_label}",
            "occurred_at": earliest,
        }, ""
    return None, "history_missing"


async def ensure_task_assignment_responsibility_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _task_assignment_responsibilities (
            parser_type VARCHAR(50) NOT NULL,
            row_key VARCHAR(200) NOT NULL,
            first_community VARCHAR(200) NOT NULL DEFAULT '',
            first_inspector VARCHAR(100) NOT NULL DEFAULT '',
            captured_by BIGINT DEFAULT NULL,
            capture_source VARCHAR(40) NOT NULL DEFAULT 'assignment',
            captured_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (parser_type,row_key),
            INDEX idx_task_first_inspector (first_community,first_inspector)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _task_internal_transfer_events (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            parser_type VARCHAR(50) NOT NULL,
            row_key VARCHAR(200) NOT NULL,
            source_id BIGINT NOT NULL,
            from_community VARCHAR(200) NOT NULL DEFAULT '',
            to_community VARCHAR(200) NOT NULL,
            from_inspector VARCHAR(100) NOT NULL DEFAULT '',
            to_leader VARCHAR(100) NOT NULL,
            operator_user_id BIGINT DEFAULT NULL,
            source_revision BIGINT UNSIGNED NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_task_transfer_task (parser_type,row_key,created_at),
            INDEX idx_task_transfer_target (to_community,to_leader,created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)


async def migrate_responsibility_row_key(
    cur,
    parser_type: str,
    row_key_before: str,
    row_key_after: str,
) -> None:
    if not row_key_before or not row_key_after or row_key_before == row_key_after:
        return
    await cur.execute(
        "UPDATE IGNORE _task_assignment_responsibilities SET row_key=%s "
        "WHERE parser_type=%s AND row_key=%s",
        (row_key_after, parser_type, row_key_before),
    )
    await cur.execute(
        "UPDATE _task_internal_transfer_events SET row_key=%s "
        "WHERE parser_type=%s AND row_key=%s",
        (row_key_after, parser_type, row_key_before),
    )


async def capture_first_assignment(
    cur,
    *,
    parser_type: str,
    row_key: str,
    community: str,
    inspector: str,
    actor_user_id: int | None = None,
    source: str = "assignment",
) -> bool:
    normalized_community = str(community or "").strip()
    normalized_inspector = str(inspector or "").strip()
    if not normalized_community or not normalized_inspector:
        return False
    await cur.execute(
        "INSERT IGNORE INTO _task_assignment_responsibilities "
        "(parser_type,row_key,first_community,first_inspector,captured_by,capture_source) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            parser_type,
            str(row_key),
            normalized_community,
            normalized_inspector,
            actor_user_id,
            source,
        ),
    )
    return cur.rowcount == 1


async def record_internal_transfer(
    cur,
    *,
    parser_type: str,
    row_key: str,
    source_id: int,
    before: dict[str, Any],
    target_community: str,
    target_leader: str,
    operator_user_id: int | None,
    source_revision: int,
    community_field: str = "社区",
) -> None:
    await cur.execute(
        "SELECT first_community,first_inspector "
        "FROM _task_assignment_responsibilities "
        "WHERE parser_type=%s AND row_key=%s FOR UPDATE",
        (parser_type, row_key),
    )
    responsibility = await cur.fetchone()
    if not responsibility:
        raise LookupError("任务缺少可靠的第一核查人责任记录，请先执行维护核验")
    await cur.execute(
        "INSERT INTO _task_internal_transfer_events "
        "(parser_type,row_key,source_id,from_community,to_community,"
        "from_inspector,to_leader,operator_user_id,source_revision) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            parser_type,
            row_key,
            source_id,
            str(before.get(community_field) or "").strip(),
            target_community,
            str(before.get("核查人") or "").strip(),
            target_leader,
            operator_user_id,
            source_revision,
        ),
    )


async def task_update_is_credited_to(
    cur,
    parser_type: str,
    row_key: str,
    inspector_name: str,
) -> bool:
    await cur.execute(
        "SELECT first_inspector FROM _task_assignment_responsibilities "
        "WHERE parser_type=%s AND row_key=%s",
        (parser_type, row_key),
    )
    row = await cur.fetchone()
    if not row:
        # Historical tasks without provable responsibility stay outside
        # personal workload until the explicit maintenance flow resolves them.
        return False
    return str(row[0] or "").strip() == str(inspector_name or "").strip()


async def audit_missing_first_assignments(
    cur,
    *,
    daily_report_schema: str,
) -> list[dict[str, Any]]:
    """Return current assigned tasks whose historical first owner is unresolved."""
    if not _SCHEMA_NAME.fullmatch(daily_report_schema):
        raise ValueError("invalid daily report schema")
    parser_types = tuple(TASK_WORKFLOWS)
    placeholders = ",".join(["%s"] * len(parser_types))
    await cur.execute(
        f"""
        SELECT projection.parser_type,projection.row_key
        FROM _online_source_projection AS projection
        LEFT JOIN _task_assignment_responsibilities AS responsibility
          ON responsibility.parser_type=projection.parser_type
         AND responsibility.row_key=projection.row_key
        WHERE projection.parser_type IN ({placeholders})
          AND TRIM(COALESCE(projection.inspector,''))<>''
          AND projection.source_count=1
          AND projection.conflict=0
          AND responsibility.row_key IS NULL
        ORDER BY projection.parser_type,projection.row_key
        """,
        parser_types,
    )
    missing_keys = [(_text(row[0]), _text(row[1])) for row in await cur.fetchall()]
    if not missing_keys:
        return []
    missing_set = set(missing_keys)
    events: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    ledger: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    await cur.execute(
        f"""
        SELECT event.parser_type,event.row_key,event.created_at,
               event.from_community,event.from_inspector
        FROM _task_internal_transfer_events AS event
        JOIN _online_source_projection AS projection
          ON projection.parser_type=event.parser_type
         AND projection.row_key=event.row_key
        LEFT JOIN _task_assignment_responsibilities AS responsibility
          ON responsibility.parser_type=event.parser_type
         AND responsibility.row_key=event.row_key
        WHERE event.parser_type IN ({placeholders})
          AND responsibility.row_key IS NULL
        ORDER BY event.created_at,event.id
        """,
        parser_types,
    )
    for parser_type, row_key, created_at, community, inspector in await cur.fetchall():
        key = (_text(parser_type), _text(row_key))
        if key not in missing_set:
            continue
        item = _candidate(
            source="internal_transfer",
            occurred_at=created_at,
            community=community,
            inspector=inspector,
        )
        if item:
            events[key].append(item)

    await cur.execute(
        f"""
        SELECT audit.parser_type,
               COALESCE(NULLIF(audit.row_key_after,''),audit.row_key_before),
               audit.created_at,audit.before_values,audit.after_values
        FROM _online_writeback_audit AS audit
        JOIN _online_source_projection AS projection
          ON projection.parser_type=audit.parser_type
         AND projection.row_key=COALESCE(
                NULLIF(audit.row_key_after,''),audit.row_key_before
             )
        LEFT JOIN _task_assignment_responsibilities AS responsibility
          ON responsibility.parser_type=projection.parser_type
         AND responsibility.row_key=projection.row_key
        WHERE audit.parser_type IN ({placeholders})
          AND audit.column_name LIKE %s
          AND responsibility.row_key IS NULL
        ORDER BY audit.created_at,audit.id
        """,
        (*parser_types, "%核查人%"),
    )
    for parser_type, row_key, created_at, before_raw, after_raw in await cur.fetchall():
        key = (_text(parser_type), _text(row_key))
        if key not in missing_set:
            continue
        before = _json_object(before_raw)
        after = _json_object(after_raw)
        if _text(before.get("核查人")) or not _text(after.get("核查人")):
            continue
        parser = TASK_WORKFLOWS.get(key[0])
        community = after.get("社区")
        if not community and parser:
            # Historical parser implementations consistently exposed the
            # normalized community as 社区 in audit snapshots.
            community = after.get("下发社区")
        item = _candidate(
            source="assignment_audit",
            occurred_at=created_at,
            community=community,
            inspector=after.get("核查人"),
        )
        if item:
            events[key].append(item)

    await cur.execute(
        f"""
        SELECT ledger.parser_type,ledger.row_key,ledger.report_date,
               ledger.community,ledger.inspector
        FROM `{daily_report_schema}`._daily_task_ledger AS ledger
        JOIN _online_source_projection AS projection
          ON projection.parser_type=ledger.parser_type
         AND projection.row_key=ledger.row_key
        LEFT JOIN _task_assignment_responsibilities AS responsibility
          ON responsibility.parser_type=ledger.parser_type
         AND responsibility.row_key=ledger.row_key
        WHERE ledger.parser_type IN ({placeholders})
          AND TRIM(COALESCE(ledger.inspector,''))<>''
          AND responsibility.row_key IS NULL
        ORDER BY ledger.report_date,ledger.updated_at
        """,
        parser_types,
    )
    for parser_type, row_key, report_date, community, inspector in await cur.fetchall():
        key = (_text(parser_type), _text(row_key))
        if key not in missing_set:
            continue
        item = _candidate(
            source="daily_ledger",
            occurred_at=report_date,
            community=community,
            inspector=inspector,
        )
        if item:
            ledger[key].append(item)

    results: list[dict[str, Any]] = []
    for parser_type, row_key in missing_keys:
        resolved, reason = resolve_first_assignment_candidate(
            events[(parser_type, row_key)],
            ledger[(parser_type, row_key)],
        )
        results.append({
            "parser_type": parser_type,
            "row_key": row_key,
            "resolved": resolved,
            "reason": reason,
        })
    return results


async def backfill_missing_first_assignments(
    conn,
    *,
    daily_report_schema: str,
) -> dict[str, Any]:
    """Backfill only historically provable first owners in one transaction."""
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await ensure_task_assignment_responsibility_schema(cur)
            rows = await audit_missing_first_assignments(
                cur,
                daily_report_schema=daily_report_schema,
            )
            inserted = 0
            for item in rows:
                resolved = item.get("resolved")
                if not resolved:
                    continue
                await cur.execute(
                    "INSERT IGNORE INTO _task_assignment_responsibilities "
                    "(parser_type,row_key,first_community,first_inspector,"
                    "capture_source,captured_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        item["parser_type"],
                        item["row_key"],
                        resolved["community"],
                        resolved["inspector"],
                        resolved["capture_source"],
                        resolved["occurred_at"],
                    ),
                )
                inserted += int(cur.rowcount == 1)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {
        "scanned": len(rows),
        "inserted": inserted,
        "unresolved": len(rows) - inserted,
        "problem_reasons": {
            reason: sum(1 for item in rows if item.get("reason") == reason)
            for reason in sorted({item.get("reason") for item in rows if item.get("reason")})
        },
    }
