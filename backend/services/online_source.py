"""腾讯在线表格来源行定位、业务投影和写回审计。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from services.parsers import get_parser
from services.task_workflow import TASK_WORKFLOWS, task_state
from services.watch_matching import (
    parse_dispatch_time,
    projection_identity,
    sync_current_task_snapshots,
)


ACTIVE_LOCAL_CHANGE_STATUSES = {"pending", "processing", "retry", "conflict"}


def json_value(value: Any, fallback):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback
    return value if isinstance(value, type(fallback)) else fallback


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_row_hash(values: dict[str, str]) -> str:
    return hashlib.sha256(stable_json(values).encode("utf-8")).hexdigest()


def match_source_cache_rows(
    existing_rows: list[dict[str, Any]],
    incoming_rows: list[dict[str, Any]],
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Pair refreshed rows without changing an existing business row's source id."""
    available = {int(row["id"]): row for row in existing_rows}
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unmatched_incoming: list[dict[str, Any]] = []

    for incoming in sorted(incoming_rows, key=lambda row: int(row["physical_row"])):
        logical = [
            row for row in available.values()
            if row["sheet_id"] == incoming["sheet_id"]
            and row["row_key"] == incoming["row_key"]
        ]
        if logical:
            existing = min(
                logical,
                key=lambda row: (
                    abs(int(row["physical_row"]) - int(incoming["physical_row"])),
                    int(row["id"]),
                ),
            )
            matched.append((existing, incoming))
            available.pop(int(existing["id"]), None)
        else:
            unmatched_incoming.append(incoming)

    still_incoming: list[dict[str, Any]] = []
    for incoming in unmatched_incoming:
        positional = next((
            row for row in available.values()
            if not row.get("has_local_changes")
            and row["sheet_id"] == incoming["sheet_id"]
            and int(row["physical_row"]) == int(incoming["physical_row"])
        ), None)
        if positional:
            matched.append((positional, incoming))
            available.pop(int(positional["id"]), None)
        else:
            still_incoming.append(incoming)

    return matched, still_incoming, list(available.values())


def sheet_lock_name(spreadsheet_id: int) -> str:
    return f"binhu_sheet_write_{int(spreadsheet_id)}"


async def resolve_source_columns(client, spreadsheet: dict, parser) -> list[str]:
    """按当前工作表表头选择解析器支持的物理列布局。"""
    return await client.resolve_column_layout(
        spreadsheet["file_id"],
        spreadsheet["data_sheet_id"],
        int(spreadsheet.get("header_row") or 1),
        parser.source_column_layouts(),
    )


async def acquire_sheet_lock(cur, spreadsheet_id: int, timeout: int = 5) -> bool:
    await cur.execute(
        "SELECT GET_LOCK(%s, %s)",
        (sheet_lock_name(spreadsheet_id), timeout),
    )
    row = await cur.fetchone()
    return bool(row and row[0] == 1)


async def release_sheet_lock(cur, spreadsheet_id: int) -> None:
    await cur.execute("SELECT RELEASE_LOCK(%s)", (sheet_lock_name(spreadsheet_id),))
    await cur.fetchone()


async def rebuild_projection(cur, parser_type: str) -> None:
    parser = get_parser(parser_type)
    await cur.execute(
        "SELECT row_key, first_dispatch_at FROM _online_source_projection WHERE parser_type=%s",
        (parser_type,),
    )
    previous_first_dispatch = {
        str(row[0]): row[1] for row in await cur.fetchall() if row[1]
    }
    await cur.execute(
        "SELECT id, row_key, values_json FROM _online_source_rows "
        "WHERE parser_type=%s ORDER BY spreadsheet_id, physical_row",
        (parser_type,),
    )
    source_records = await cur.fetchall()
    source_ids = [int(row[0]) for row in source_records]
    local_by_source: dict[int, dict[str, str]] = defaultdict(dict)
    if source_ids:
        placeholders = ", ".join(["%s"] * len(source_ids))
        await cur.execute(
            f"SELECT source_id, field_name, local_value "
            f"FROM _online_local_changes WHERE source_id IN ({placeholders}) "
            f"AND status IN ('pending','processing','retry','conflict')",
            source_ids,
        )
        for source_id, field_name, local_value in await cur.fetchall():
            local_by_source[int(source_id)][str(field_name)] = str(local_value or "")
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for source_id, row_key, raw_values in source_records:
        values = json_value(raw_values, {})
        values.update(local_by_source.get(int(source_id), {}))
        grouped[str(row_key)].append({
            column: str(values.get(column, "") or "").strip()
            for column in parser.COLUMNS
        })

    await cur.execute(
        "SELECT row_key_before, row_key_after FROM _online_writeback_audit "
        "WHERE parser_type=%s AND sync_status='pending'",
        (parser_type,),
    )
    pending_states = {
        str(value): "pending"
        for row in await cur.fetchall()
        for value in row
        if value
    }
    await cur.execute(
        "SELECT row_key, status FROM _online_local_changes "
        "WHERE parser_type=%s "
        "AND status IN ('pending','processing','retry','conflict')",
        (parser_type,),
    )
    state_priority = {"pending": 1, "processing": 1, "retry": 2, "conflict": 3}
    for row_key, status in await cur.fetchall():
        key = str(row_key)
        candidate = str(status)
        current = pending_states.get(key, "")
        if state_priority.get(candidate, 0) > state_priority.get(current, 0):
            pending_states[key] = "pending" if candidate == "processing" else candidate
    if parser_type == "全链条":
        await cur.execute("""
            SELECT source.row_key
            FROM _police_dispatch_publish_results AS result
            JOIN _online_source_rows AS source
              ON source.spreadsheet_id=result.spreadsheet_id
             AND source.sheet_id=result.sheet_id
             AND source.physical_row=result.physical_row
            WHERE result.status='success'
        """)
        pending_states.update({
            str(row[0]): "pending" for row in await cur.fetchall() if row[0]
        })

    projection_rows = []
    for row_key, source_rows in grouped.items():
        parent = dict(source_rows[0])
        conflict = False
        for incoming in source_rows[1:]:
            if incoming == parent:
                continue
            merged = parser.merge_duplicate_row(parent, incoming)
            if merged is None:
                conflict = True
                continue
            parent = merged
        projection_rows.append((
            parser_type,
            row_key,
            stable_json(parent),
            parser.community_value(parent),
            str(parent.get("核查人", "") or "").strip(),
            projection_identity(parser_type, parent, parser.COLUMNS),
            parse_dispatch_time(
                parent,
                list(dict.fromkeys([
                    *(
                        list(getattr(TASK_WORKFLOWS.get(parser_type), "date_fields", []))
                        if parser_type in TASK_WORKFLOWS else []
                    ),
                    "下发日期",
                    "下发时间",
                    "创建时间",
                    "日期",
                ])),
                previous_first_dispatch.get(row_key),
            ),
            task_state(parser_type, parent),
            len(source_rows),
            int(conflict),
            "\n".join(str(parent.get(column, "") or "") for column in parser.COLUMNS),
            pending_states.get(row_key, ""),
        ))

    await cur.execute(
        "DELETE FROM _online_source_projection WHERE parser_type=%s",
        (parser_type,),
    )
    if projection_rows:
        await cur.executemany(
            """
            INSERT INTO _online_source_projection (
                parser_type, row_key, values_json, community, inspector,
                identity_hmac, first_dispatch_at, task_state,
                source_count, conflict, search_text, pending_state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            projection_rows,
        )
    await sync_current_task_snapshots(cur, parser_type)


async def replace_source_cache(
    conn,
    spreadsheet: dict,
    source_rows: list[dict],
) -> None:
    """增量吸收腾讯读取结果，保留来源 id 并合并平台待写回字段。"""
    parser = get_parser(spreadsheet["parser_type"])
    prepared = []
    for source in source_rows:
        values = parser.normalize_source_row(source.get("values", {}))
        metadata = {
            column: (source.get("cell_meta") or {}).get(column, {"type": "text"})
            for column in parser.COLUMNS
        }
        prepared.append({
            "spreadsheet_id": int(spreadsheet["id"]),
            "parser_type": spreadsheet["parser_type"],
            "sheet_id": str(spreadsheet["data_sheet_id"]),
            "physical_row": int(source["physical_row"]),
            "row_key": parser.make_row_key(values),
            "row_hash": source_row_hash(values),
            "values": values,
            "values_json": stable_json(values),
            "cell_meta_json": stable_json(metadata),
        })

    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, sheet_id, physical_row, row_key, row_hash,
                       values_json, cell_meta_json, revision
                FROM _online_source_rows
                WHERE spreadsheet_id=%s
                FOR UPDATE
                """,
                (spreadsheet["id"],),
            )
            existing = [
                {
                    "id": int(row[0]),
                    "sheet_id": str(row[1]),
                    "physical_row": int(row[2]),
                    "row_key": str(row[3]),
                    "row_hash": str(row[4]),
                    "values_json": row[5],
                    "cell_meta_json": row[6],
                    "revision": int(row[7]),
                    "has_local_changes": False,
                }
                for row in await cur.fetchall()
            ]
            existing_ids = [row["id"] for row in existing]
            changes_by_source: dict[int, list[dict[str, Any]]] = defaultdict(list)
            if existing_ids:
                placeholders = ", ".join(["%s"] * len(existing_ids))
                await cur.execute(
                    f"""
                    SELECT id, source_id, audit_id, field_name, base_value,
                           local_value, status
                    FROM _online_local_changes
                    WHERE source_id IN ({placeholders})
                      AND status IN ('pending','processing','retry','conflict')
                    FOR UPDATE
                    """,
                    existing_ids,
                )
                for change in await cur.fetchall():
                    changes_by_source[int(change[1])].append({
                        "id": int(change[0]),
                        "audit_id": int(change[2]),
                        "field_name": str(change[3]),
                        "base_value": str(change[4] or ""),
                        "local_value": str(change[5] or ""),
                        "status": str(change[6]),
                    })
                for row in existing:
                    row["has_local_changes"] = bool(changes_by_source.get(row["id"]))

            matched, incoming_only, existing_only = match_source_cache_rows(
                existing, prepared
            )
            if existing_ids:
                await cur.execute(
                    "UPDATE _online_source_rows SET physical_row=-id "
                    "WHERE spreadsheet_id=%s",
                    (spreadsheet["id"],),
                )

            affected_audits: set[int] = set()
            for old, incoming in matched:
                changed = any((
                    old["sheet_id"] != incoming["sheet_id"],
                    int(old["physical_row"]) != int(incoming["physical_row"]),
                    old["row_hash"] != incoming["row_hash"],
                    stable_json(json_value(old["cell_meta_json"], {}))
                    != incoming["cell_meta_json"],
                ))
                await cur.execute(
                    """
                    UPDATE _online_source_rows
                    SET parser_type=%s, sheet_id=%s, physical_row=%s,
                        row_key=%s, row_hash=%s, values_json=%s,
                        cell_meta_json=%s, revision=%s,
                        refreshed_at=UTC_TIMESTAMP()
                    WHERE id=%s
                    """,
                    (
                        incoming["parser_type"], incoming["sheet_id"],
                        incoming["physical_row"], incoming["row_key"],
                        incoming["row_hash"], incoming["values_json"],
                        incoming["cell_meta_json"],
                        old["revision"] + (1 if changed else 0), old["id"],
                    ),
                )
                for change in changes_by_source.get(old["id"], []):
                    affected_audits.add(change["audit_id"])
                    remote = str(incoming["values"].get(change["field_name"], "") or "")
                    if remote == change["local_value"]:
                        await cur.execute(
                            "DELETE FROM _online_local_changes WHERE id=%s",
                            (change["id"],),
                        )
                    elif remote != change["base_value"]:
                        await cur.execute(
                            """
                            UPDATE _online_local_changes
                            SET status='conflict', remote_value=%s,
                                error_code='field_changed',
                                last_error='腾讯同一字段已被修改'
                            WHERE id=%s
                            """,
                            (remote, change["id"]),
                        )
                    elif change["status"] == "conflict":
                        await cur.execute(
                            """
                            UPDATE _online_local_changes
                            SET status='pending', remote_value=NULL,
                                attempt_count=0, next_attempt_at=NULL,
                                error_code='', last_error=''
                            WHERE id=%s
                            """,
                            (change["id"],),
                        )

            for old in existing_only:
                changes = changes_by_source.get(old["id"], [])
                if changes:
                    affected_audits.update(change["audit_id"] for change in changes)
                    await cur.execute(
                        """
                        UPDATE _online_local_changes
                        SET status='conflict', remote_value=NULL,
                            error_code='source_missing',
                            last_error='腾讯来源行已删除或业务主键已变化'
                        WHERE source_id=%s
                          AND status IN ('pending','processing','retry','conflict')
                        """,
                        (old["id"],),
                    )
                else:
                    await cur.execute(
                        "DELETE FROM _online_source_rows WHERE id=%s",
                        (old["id"],),
                    )

            if incoming_only:
                await cur.executemany(
                    """
                    INSERT INTO _online_source_rows (
                        spreadsheet_id, parser_type, sheet_id, physical_row,
                        row_key, row_hash, values_json, cell_meta_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [(
                        row["spreadsheet_id"], row["parser_type"], row["sheet_id"],
                        row["physical_row"], row["row_key"], row["row_hash"],
                        row["values_json"], row["cell_meta_json"],
                    ) for row in incoming_only],
                )

            for audit_id in affected_audits:
                await cur.execute(
                    "SELECT status FROM _online_local_changes WHERE audit_id=%s",
                    (audit_id,),
                )
                statuses = {str(row[0]) for row in await cur.fetchall()}
                if "conflict" in statuses:
                    audit_status = "conflict"
                elif statuses & ACTIVE_LOCAL_CHANGE_STATUSES:
                    audit_status = "pending"
                else:
                    audit_status = "synced"
                await cur.execute(
                    "UPDATE _online_writeback_audit SET sync_status=%s, "
                    "synced_at=IF(%s='synced',UTC_TIMESTAMP(),synced_at) "
                    "WHERE id=%s",
                    (audit_status, audit_status, audit_id),
                )
            await cur.execute(
                """
                INSERT INTO _online_source_cache_state (
                    spreadsheet_id, parser_type, row_count, refreshed_at
                ) VALUES (%s, %s, %s, UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE
                    parser_type=VALUES(parser_type),
                    row_count=VALUES(row_count),
                    refreshed_at=UTC_TIMESTAMP()
                """,
                (spreadsheet["id"], spreadsheet["parser_type"], len(prepared)),
            )
            await rebuild_projection(cur, spreadsheet["parser_type"])
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def update_cached_source_row(
    conn,
    source_id: int,
    parser_type: str,
    values: dict[str, str],
    metadata: dict,
) -> tuple[str, int]:
    parser = get_parser(parser_type)
    row_key = parser.make_row_key(values)
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE _online_source_rows
                SET row_key=%s, row_hash=%s, values_json=%s,
                    cell_meta_json=%s, revision=revision+1,
                    refreshed_at=UTC_TIMESTAMP()
                WHERE id=%s AND parser_type=%s
                """,
                (
                    row_key,
                    source_row_hash(values),
                    stable_json(values),
                    stable_json(metadata),
                    source_id,
                    parser_type,
                ),
            )
            if cur.rowcount != 1:
                raise LookupError("来源行已变化，请刷新后重试")
            await cur.execute(
                "SELECT revision FROM _online_source_rows WHERE id=%s",
                (source_id,),
            )
            revision = int((await cur.fetchone())[0])
            await rebuild_projection(cur, parser_type)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return row_key, revision


async def mark_writebacks_synced(cur, spreadsheet_id: int) -> None:
    await cur.execute(
        """
        UPDATE _online_writeback_audit
        SET sync_status='synced', synced_at=UTC_TIMESTAMP()
        WHERE spreadsheet_id=%s AND sync_status='pending'
        """,
        (spreadsheet_id,),
    )
    await cur.execute(
        "UPDATE _police_dispatch_publish_results "
        "SET status='synced' "
        "WHERE spreadsheet_id=%s AND status='success'",
        (spreadsheet_id,),
    )


async def cleanup_expired_writeback_audit(cur) -> int:
    await cur.execute(
        "DELETE FROM _online_writeback_audit "
        "WHERE created_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 90 DAY)"
    )
    return int(cur.rowcount or 0)
