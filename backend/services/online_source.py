"""腾讯在线表格来源行定位、业务投影和写回审计。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from services.parsers import get_parser
from services.task_workflow import TASK_WORKFLOWS, task_state
from services.task_registration import (
    ensure_missing_registration_review,
    registration_links_by_rows,
)
from services.task_graph import (
    reconcile_projection_task_graph,
    reconcile_projection_task_graph_rows,
)
from services.watch_matching import (
    parse_dispatch_time,
    projection_identity,
    sync_current_task_snapshots_for_keys,
    sync_current_task_snapshots,
)
from services.local_source import local_data_source_enabled
from services.address_matching import RuleMatcher
from services.address_match_feedback import (
    apply_feedback_memory,
    load_feedback_memories,
)


ACTIVE_LOCAL_CHANGE_STATUSES = {"pending", "processing", "retry", "conflict"}


async def _address_match_entries(cur) -> list[dict[str, Any]]:
    """读取启用的小区地址库，供投影重建时生成建议。"""
    await cur.execute(
        f"""
        SELECT entry.id, entry.name, entry.normalized_name, entry.detail_address,
               entry.aliases_json, entry.community_id, community.name,
               entry.enabled
        FROM _police_address_entries AS entry
        LEFT JOIN _communities AS community ON community.id=entry.community_id
        WHERE entry.enabled=1
        ORDER BY entry.id
        """
    )
    result = []
    for row in await cur.fetchall():
        result.append({
            "id": int(row[0]),
            "name": str(row[1] or ""),
            "normalized_name": str(row[2] or ""),
            "detail_address": str(row[3] or ""),
            "aliases_json": row[4],
            "community_id": int(row[5]) if row[5] is not None else None,
            "community_name": str(row[6] or ""),
            "enabled": bool(row[7]),
        })
    return result


def _task_match_address(parser_type: str, values: dict[str, str]) -> str:
    workflow = TASK_WORKFLOWS.get(parser_type)
    address_fields = tuple(
        field
        for field in getattr(workflow, "address_fields", ())
        if field != "现住址"
    ) or tuple(getattr(workflow, "address_fields", ()))
    return next((
        str(values.get(field) or "").strip()
        for field in address_fields
        if str(values.get(field) or "").strip()
    ), "")


def active_source_sql_filter(parser_type: str, alias: str = "source") -> str:
    """Limit task-facing source queries to the active data ownership model.

    Historical external rows may remain archived for audit purposes, but no
    task-facing query may treat them as active business sources.
    """
    if local_data_source_enabled():
        prefix = f"{alias}."
        local_kinds = (
            f"{prefix}source_kind IN ('local_table','local_dispatch')"
        )
        return (
            f" AND {prefix}spreadsheet_id=0"
            f" AND {local_kinds}"
        )
    return ""


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


def assignment_projection_fields(
    parser_type: str,
    values: dict[str, str],
    *,
    community: str,
    source_count: int,
    conflict: bool,
    task_state_value: str,
) -> tuple[str, str, str, int]:
    """Return the small, indexed projection used by the assignment workbench.

    Keep the display values deliberately derived from the parser summary so the
    workbench does not need to deserialize the complete source JSON.  The sort
    key is intentionally conservative: case-folding and whitespace removal are
    stable across MySQL collations and match the previous UI ordering closely.
    """
    summary = TASK_WORKFLOWS[parser_type].summary(values)
    source_label = str(summary.get("source") or TASK_WORKFLOWS[parser_type].label).strip()
    address_display = str(summary.get("address") or "未填写地址").strip()
    sort_key = "".join(address_display.casefold().split())
    queue_ready = int(
        not str(values.get("核查人") or "").strip()
        and task_state_value != "completed"
        and int(source_count or 0) == 1
        and not conflict
    )
    return source_label, address_display, sort_key, queue_ready


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
            and row.get("expected_row_key", row["row_key"])
            == incoming["row_key"]
        ]
        if logical:
            existing = min(
                logical,
                key=lambda row: (
                    0 if row.get("has_local_changes") else 1,
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


async def rebuild_projection(
    cur,
    parser_type: str,
    *,
    reconcile_graph: bool = True,
    row_keys: list[str] | None = None,
) -> None:
    """Rebuild all projections or only the explicitly affected task rows.

    Interactive edits must pass ``row_keys`` so a single save never deletes,
    rematches and recreates an entire business projection.  Imports and
    maintenance jobs intentionally omit it and keep the full rebuild behavior.
    """
    parser = get_parser(parser_type)
    target_keys = list(dict.fromkeys(
        str(row_key).strip() for row_key in (row_keys or [])
        if str(row_key).strip()
    ))
    key_filter = ""
    key_params: list[str] = []
    if target_keys:
        placeholders = ",".join(["%s"] * len(target_keys))
        key_filter = f" AND row_key IN ({placeholders})"
        key_params = target_keys
    matcher = RuleMatcher()
    address_entries = await _address_match_entries(cur)
    address_entries_by_id = {int(item["id"]): item for item in address_entries}
    await cur.execute(
        f"""
        SELECT row_key, original_address, suggested_entry_id,
               suggested_community_id, suggested_community_name,
               match_status, match_score, match_method, match_reason,
               candidates_json, matcher_version, confirmed_entry_id,
               confirmed_by, confirmed_at
        FROM _online_task_address_matches
        WHERE parser_type=%s
        {key_filter}
        """,
        (parser_type, *key_params),
    )
    stored_matches = {
        str(row[0]): {
            "original_address": str(row[1] or ""),
            "suggested_entry_id": row[2],
            "suggested_community_id": row[3],
            "suggested_community_name": str(row[4] or ""),
            "status": str(row[5] or "unmatched"),
            "score": float(row[6] or 0),
            "method": str(row[7] or ""),
            "reason": str(row[8] or ""),
            "candidates": json_value(row[9], []),
            "version": str(row[10] or ""),
            "confirmed_entry_id": int(row[11]) if row[11] is not None else None,
            "confirmed_by": row[12],
            "confirmed_at": row[13],
        }
        for row in await cur.fetchall()
    }
    await cur.execute(
        "SELECT row_key, first_dispatch_at FROM _online_source_projection "
        f"WHERE parser_type=%s{key_filter}",
        (parser_type, *key_params),
    )
    previous_first_dispatch = {
        str(row[0]): row[1] for row in await cur.fetchall() if row[1]
    }
    source_key_filter = (
        f" AND source.row_key IN ({','.join(['%s'] * len(target_keys))})"
        if target_keys else ""
    )
    await cur.execute(
        "SELECT source.id, source.row_key, source.values_json, "
        "source.revision, source.row_hash "
        "FROM _online_source_rows AS source WHERE source.parser_type=%s "
        "AND source.archived_at IS NULL"
        f"{source_key_filter}"
        f"{active_source_sql_filter(parser_type)} "
        "ORDER BY spreadsheet_id, physical_row",
        (parser_type, *target_keys),
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
    source_contexts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_revisions: dict[str, int] = {}
    for source_id, row_key, raw_values, revision, row_hash in source_records:
        values = json_value(raw_values, {})
        values.update(local_by_source.get(int(source_id), {}))
        grouped[str(row_key)].append({
            column: str(values.get(column, "") or "").strip()
            for column in parser.COLUMNS
        })
        source_contexts[str(row_key)].append({
            "id": int(source_id),
            "revision": int(revision),
            "row_hash": str(row_hash or ""),
        })
        source_revisions[str(row_key)] = max(
            source_revisions.get(str(row_key), 0), int(revision)
        )

    audit_filter = ""
    audit_params: list[object] = [parser_type]
    if target_keys:
        placeholders = ",".join(["%s"] * len(target_keys))
        audit_filter = (
            f" AND (row_key_before IN ({placeholders}) "
            f"OR row_key_after IN ({placeholders}))"
        )
        audit_params.extend(target_keys)
        audit_params.extend(target_keys)
    await cur.execute(
        "SELECT row_key_before, row_key_after FROM _online_writeback_audit "
        f"WHERE parser_type=%s AND sync_status='pending'{audit_filter}",
        audit_params,
    )
    pending_states = {
        str(value): "pending"
        for row in await cur.fetchall()
        for value in row
        if value
    }
    local_change_filter = ""
    local_change_params: list[object] = [parser_type]
    if target_keys:
        placeholders = ",".join(["%s"] * len(target_keys))
        local_change_filter = f" AND row_key IN ({placeholders})"
        local_change_params.extend(target_keys)
    await cur.execute(
        "SELECT row_key, status FROM _online_local_changes "
        "WHERE parser_type=%s "
        "AND status IN ('pending','processing','retry','conflict')"
        f"{local_change_filter}",
        local_change_params,
    )
    state_priority = {"pending": 1, "processing": 1, "retry": 2, "conflict": 3}
    for row_key, status in await cur.fetchall():
        key = str(row_key)
        candidate = str(status)
        current = pending_states.get(key, "")
        if state_priority.get(candidate, 0) > state_priority.get(current, 0):
            pending_states[key] = "pending" if candidate == "processing" else candidate
    if parser_type == "全链条":
        police_filter = ""
        police_params: list[object] = []
        if target_keys:
            placeholders = ",".join(["%s"] * len(target_keys))
            police_filter = f" AND source.row_key IN ({placeholders})"
            police_params.extend(target_keys)
        await cur.execute(f"""
            SELECT source.row_key
            FROM _police_dispatch_publish_results AS result
            JOIN _online_source_rows AS source
              ON source.spreadsheet_id=result.spreadsheet_id
             AND source.sheet_id=result.sheet_id
             AND source.physical_row=result.physical_row
            WHERE result.status='success'
            {police_filter}
        """, police_params)
        pending_states.update({
            str(row[0]): "pending" for row in await cur.fetchall() if row[0]
        })

    projection_rows = []
    address_match_rows = []
    merged_rows: dict[str, tuple[dict[str, str], bool]] = {}
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
        merged_rows[row_key] = (parent, conflict)
        identity_hmac = projection_identity(parser_type, parent, parser.COLUMNS)
        await ensure_missing_registration_review(
            cur,
            parser_type=parser_type,
            row_key=row_key,
            values=parent,
            source_contexts=source_contexts.get(row_key, []),
            identity_hmac=identity_hmac,
            task_community=parser.community_value(parent),
        )

    registration_links = await registration_links_by_rows(
        cur,
        parser_type,
        list(grouped),
    )
    task_addresses = {
        row_key: _task_match_address(parser_type, parent)
        for row_key, (parent, _) in merged_rows.items()
    }
    feedback_memories = await load_feedback_memories(cur, (
        (task_addresses.get(row_key, ""), parser.community_value(parent))
        for row_key, (parent, _) in merged_rows.items()
    ))

    for row_key, source_rows in grouped.items():
        parent, conflict = merged_rows[row_key]
        address = task_addresses.get(row_key, "")
        address_match = matcher.match(
            address,
            address_entries,
            community_name=parser.community_value(parent),
        )
        stored = stored_matches.get(row_key)
        if stored and stored.get("status") == "confirmed":
            confirmed_entry = address_entries_by_id.get(
                int(stored.get("confirmed_entry_id") or 0)
            )
            if confirmed_entry:
                address_match = {
                    "status": "confirmed",
                    "score": float(stored.get("score") or 1),
                    "method": "人工确认",
                    "reason": str(stored.get("reason") or "管理员已确认小区归属"),
                    "candidate": {
                        "entry_id": int(confirmed_entry["id"]),
                        "name": str(confirmed_entry.get("name") or ""),
                        "community_id": confirmed_entry.get("community_id"),
                        "community_name": str(confirmed_entry.get("community_name") or ""),
                        "score": float(stored.get("score") or 1),
                        "method": "人工确认",
                        "reason": "管理员已确认小区归属",
                    },
                    "candidates": address_match.get("candidates") or stored.get("candidates") or [],
                    "version": str(stored.get("version") or matcher.version),
                }
            else:
                address_match = {
                    "status": "conflict",
                    "score": float(stored.get("score") or 0),
                    "method": "人工确认复核",
                    "reason": "已确认的小区已停用或不存在，需要重新确认",
                    "candidate": None,
                    "candidates": stored.get("candidates") or [],
                    "version": str(stored.get("version") or matcher.version),
                }
        else:
            address_match = apply_feedback_memory(
                address_match,
                address=address,
                community_name=parser.community_value(parent),
                memories=feedback_memories,
                entries_by_id=address_entries_by_id,
            )
        match_candidate = address_match.get("candidate") or {}
        projected_task_state = task_state(
            parser_type,
            parent,
            registration_status=str(
                (registration_links.get(row_key) or {}).get("status") or ""
            ),
        )
        assignment_source_label, assignment_address_display, assignment_address_sort_key, assignment_queue_ready = assignment_projection_fields(
            parser_type,
            parent,
            community=parser.community_value(parent),
            source_count=len(source_rows),
            conflict=conflict,
            task_state_value=projected_task_state,
        )
        address_match_rows.append((
            parser_type,
            row_key,
            address,
            match_candidate.get("entry_id"),
            match_candidate.get("community_id"),
            match_candidate.get("community_name", ""),
            address_match.get("status", "unmatched"),
            address_match.get("score", 0.0),
            address_match.get("method", ""),
            address_match.get("reason", ""),
            stable_json(address_match.get("candidates", [])),
            address_match.get("version", matcher.version),
            stored.get("confirmed_entry_id") if stored else None,
            stored.get("confirmed_by") if stored else None,
            stored.get("confirmed_at") if stored else None,
        ))
        projection_rows.append((
            parser_type,
            row_key,
            stable_json(parent),
            parser.community_value(parent),
            match_candidate.get("entry_id"),
            match_candidate.get("name", ""),
            address_match.get("status", "unmatched"),
            address_match.get("score", 0.0),
            address_match.get("method", ""),
            address_match.get("reason", ""),
            stable_json(address_match.get("candidates", [])),
            address_match.get("version", ""),
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
            projected_task_state,
            len(source_rows),
            int(conflict),
            "\n".join(str(parent.get(column, "") or "") for column in parser.COLUMNS),
            pending_states.get(row_key, ""),
            assignment_source_label,
            assignment_address_display,
            assignment_address_sort_key,
            assignment_queue_ready,
            source_revisions.get(row_key, 0),
        ))

    if address_match_rows:
        await cur.executemany(
            """
            INSERT INTO _online_task_address_matches (
                parser_type, row_key, original_address,
                suggested_entry_id, suggested_community_id,
                suggested_community_name, match_status, match_score,
                match_method, match_reason, candidates_json, matcher_version,
                confirmed_entry_id, confirmed_by, confirmed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                original_address=VALUES(original_address),
                suggested_entry_id=VALUES(suggested_entry_id),
                suggested_community_id=VALUES(suggested_community_id),
                suggested_community_name=VALUES(suggested_community_name),
                match_status=VALUES(match_status),
                match_score=VALUES(match_score),
                match_method=VALUES(match_method),
                match_reason=VALUES(match_reason),
                candidates_json=VALUES(candidates_json),
                matcher_version=VALUES(matcher_version),
                confirmed_entry_id=VALUES(confirmed_entry_id),
                confirmed_by=VALUES(confirmed_by),
                confirmed_at=VALUES(confirmed_at)
            """,
            address_match_rows,
        )

    if target_keys:
        placeholders = ",".join(["%s"] * len(target_keys))
        await cur.execute(
            "DELETE FROM _online_source_projection "
            f"WHERE parser_type=%s AND row_key IN ({placeholders})",
            (parser_type, *target_keys),
        )
    else:
        await cur.execute(
            "DELETE FROM _online_source_projection WHERE parser_type=%s",
            (parser_type,),
        )
    if projection_rows:
        await cur.executemany(
            """
            INSERT INTO _online_source_projection (
                parser_type, row_key, values_json, community,
                small_community_id, small_community_name,
                address_match_status, address_match_score,
                address_match_method, address_match_reason,
                address_match_candidates, address_match_version, inspector,
                identity_hmac, first_dispatch_at, task_state,
                source_count, conflict, search_text, pending_state,
                assignment_source_label, assignment_address_display,
                assignment_address_sort_key, assignment_queue_ready,
                source_revision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                source_revision=VALUES(source_revision),
                values_json=VALUES(values_json), community=VALUES(community),
                small_community_id=VALUES(small_community_id),
                small_community_name=VALUES(small_community_name),
                address_match_status=VALUES(address_match_status),
                address_match_score=VALUES(address_match_score),
                address_match_method=VALUES(address_match_method),
                address_match_reason=VALUES(address_match_reason),
                address_match_candidates=VALUES(address_match_candidates),
                address_match_version=VALUES(address_match_version),
                inspector=VALUES(inspector), identity_hmac=VALUES(identity_hmac),
                first_dispatch_at=COALESCE(_online_source_projection.first_dispatch_at, VALUES(first_dispatch_at)),
                task_state=VALUES(task_state), source_count=VALUES(source_count),
                conflict=VALUES(conflict), search_text=VALUES(search_text),
                pending_state=VALUES(pending_state),
                assignment_source_label=VALUES(assignment_source_label),
                assignment_address_display=VALUES(assignment_address_display),
                assignment_address_sort_key=VALUES(assignment_address_sort_key),
                assignment_queue_ready=VALUES(assignment_queue_ready)
            """,
            projection_rows,
        )
    await sync_current_task_snapshots(cur, parser_type, target_keys or None)
    if reconcile_graph:
        if target_keys:
            await reconcile_projection_task_graph_rows(cur, parser_type, target_keys)
        else:
            await reconcile_projection_task_graph(cur, parser_type)


async def rebuild_projection_rows(
    cur,
    parser_type: str,
    row_keys: list[str],
    *,
    reconcile_graph: bool = True,
) -> None:
    """Explicit incremental projection API for saves and assignments."""
    normalized = list(dict.fromkeys(
        str(row_key).strip() for row_key in row_keys if str(row_key).strip()
    ))
    if not normalized:
        return
    await rebuild_projection(
        cur,
        parser_type,
        reconcile_graph=reconcile_graph,
        row_keys=normalized,
    )


async def rebuild_projection_keys(
    cur,
    parser_type: str,
    row_keys: list[str],
    *,
    reconcile_graph: bool = False,
) -> dict[str, int]:
    """Recompute only the supplied business keys.

    This is the request-path projection primitive.  It intentionally never
    scans or deletes the rest of a parser's projection; full rebuilds remain a
    maintenance operation for repair and migration commands.
    """
    keys = sorted({str(key) for key in row_keys if str(key)})
    if not keys:
        return {"processed": 0, "deleted": 0}
    parser = get_parser(parser_type)
    placeholders = ",".join(["%s"] * len(keys))
    await cur.execute(
        f"SELECT row_key, first_dispatch_at FROM _online_source_projection "
        f"WHERE parser_type=%s AND row_key IN ({placeholders})",
        (parser_type, *keys),
    )
    first_dispatch_by_key = {str(row[0]): row[1] for row in await cur.fetchall()}
    await cur.execute(
        f"SELECT row_key, original_address, suggested_entry_id, suggested_community_id, "
        "suggested_community_name, match_status, match_score, match_method, match_reason, "
        "candidates_json, matcher_version, confirmed_entry_id, confirmed_by, confirmed_at "
        f"FROM _online_task_address_matches WHERE parser_type=%s AND row_key IN ({placeholders})",
        (parser_type, *keys),
    )
    stored_matches = {
        str(row[0]): {
            "original_address": str(row[1] or ""), "suggested_entry_id": row[2],
            "suggested_community_id": row[3], "suggested_community_name": str(row[4] or ""),
            "status": str(row[5] or "unmatched"), "score": float(row[6] or 0),
            "method": str(row[7] or ""), "reason": str(row[8] or ""),
            "candidates": json_value(row[9], []), "version": str(row[10] or ""),
            "confirmed_entry_id": int(row[11]) if row[11] is not None else None,
            "confirmed_by": row[12], "confirmed_at": row[13],
        } for row in await cur.fetchall()
    }
    address_entries = await _address_match_entries(cur)
    address_entries_by_id = {int(item["id"]): item for item in address_entries}
    await cur.execute(
        f"SELECT source.id, source.row_key, source.values_json, source.revision, source.row_hash "
        f"FROM _online_source_rows source WHERE source.parser_type=%s AND source.row_key IN ({placeholders}) "
        "AND source.archived_at IS NULL" + active_source_sql_filter(parser_type) + " ORDER BY source.id",
        (parser_type, *keys),
    )
    source_records = await cur.fetchall()
    source_ids = [int(row[0]) for row in source_records]
    local_by_source: dict[int, dict[str, str]] = defaultdict(dict)
    if source_ids:
        source_placeholders = ",".join(["%s"] * len(source_ids))
        await cur.execute(
            f"SELECT source_id, field_name, local_value FROM _online_local_changes "
            f"WHERE source_id IN ({source_placeholders}) AND status IN ('pending','processing','retry','conflict')",
            source_ids,
        )
        for source_id, field_name, local_value in await cur.fetchall():
            local_by_source[int(source_id)][str(field_name)] = str(local_value or "")
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    source_contexts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_revisions: dict[str, int] = {}
    for source_id, row_key, raw_values, revision, row_hash in source_records:
        values = json_value(raw_values, {})
        values.update(local_by_source.get(int(source_id), {}))
        grouped[str(row_key)].append({column: str(values.get(column, "") or "").strip() for column in parser.COLUMNS})
        source_contexts[str(row_key)].append({"id": int(source_id), "revision": int(revision), "row_hash": str(row_hash or "")})
        source_revisions[str(row_key)] = max(
            source_revisions.get(str(row_key), 0), int(revision)
        )
    registration_links = await registration_links_by_rows(cur, parser_type, keys)
    matcher = RuleMatcher()
    projection_rows: list[tuple] = []
    address_rows: list[tuple] = []
    prepared_rows: dict[str, tuple[dict[str, str], bool, str, str, Any]] = {}
    for row_key in keys:
        source_rows = grouped.get(row_key, [])
        if not source_rows:
            await cur.execute("DELETE FROM _online_source_projection WHERE parser_type=%s AND row_key=%s", (parser_type, row_key))
            await cur.execute("DELETE FROM _online_task_address_matches WHERE parser_type=%s AND row_key=%s", (parser_type, row_key))
            continue
        parent = dict(source_rows[0])
        conflict = False
        for incoming in source_rows[1:]:
            if incoming == parent:
                continue
            merged = parser.merge_duplicate_row(parent, incoming)
            if merged is None:
                conflict = True
            else:
                parent = merged
        identity_hmac = projection_identity(parser_type, parent, parser.COLUMNS)
        workflow = TASK_WORKFLOWS.get(parser_type)
        address = _task_match_address(parser_type, parent)
        prepared_rows[row_key] = (parent, conflict, identity_hmac, address, workflow)

    feedback_memories = await load_feedback_memories(cur, (
        (address, parser.community_value(parent))
        for parent, _, _, address, _ in prepared_rows.values()
    ))
    for row_key, prepared in prepared_rows.items():
        parent, conflict, identity_hmac, address, workflow = prepared
        await ensure_missing_registration_review(
            cur, parser_type=parser_type, row_key=row_key, values=parent,
            source_contexts=source_contexts.get(row_key, []), identity_hmac=identity_hmac,
            task_community=parser.community_value(parent),
        )
        address_match = matcher.match(address, address_entries, community_name=parser.community_value(parent))
        stored = stored_matches.get(row_key)
        if stored and stored.get("status") == "confirmed":
            confirmed = address_entries_by_id.get(int(stored.get("confirmed_entry_id") or 0))
            if confirmed:
                address_match = {"status": "confirmed", "score": float(stored.get("score") or 1), "method": "人工确认", "reason": str(stored.get("reason") or "管理员已确认小区归属"), "candidate": {"entry_id": int(confirmed["id"]), "name": str(confirmed.get("name") or ""), "community_id": confirmed.get("community_id"), "community_name": str(confirmed.get("community_name") or ""), "score": float(stored.get("score") or 1), "method": "人工确认", "reason": "管理员已确认小区归属"}, "candidates": stored.get("candidates") or [], "version": str(stored.get("version") or matcher.version)}
            else:
                address_match = {"status": "conflict", "score": float(stored.get("score") or 0), "method": "人工确认复核", "reason": "已确认的小区已停用或不存在，需要重新确认", "candidate": None, "candidates": stored.get("candidates") or [], "version": str(stored.get("version") or matcher.version)}
        else:
            address_match = apply_feedback_memory(
                address_match,
                address=address,
                community_name=parser.community_value(parent),
                memories=feedback_memories,
                entries_by_id=address_entries_by_id,
            )
        candidate = address_match.get("candidate") or {}
        address_rows.append((parser_type, row_key, address, candidate.get("entry_id"), candidate.get("community_id"), candidate.get("community_name", ""), address_match.get("status", "unmatched"), address_match.get("score", 0), address_match.get("method", ""), address_match.get("reason", ""), stable_json(address_match.get("candidates", [])), address_match.get("version", matcher.version), stored.get("confirmed_entry_id") if stored else None, stored.get("confirmed_by") if stored else None, stored.get("confirmed_at") if stored else None))
        projected_task_state = task_state(
            parser_type,
            parent,
            registration_status=str((registration_links.get(row_key) or {}).get("status") or ""),
        )
        assignment_source_label, assignment_address_display, assignment_address_sort_key, assignment_queue_ready = assignment_projection_fields(
            parser_type,
            parent,
            community=parser.community_value(parent),
            source_count=len(source_rows),
            conflict=conflict,
            task_state_value=projected_task_state,
        )
        projection_rows.append((parser_type, row_key, stable_json(parent), parser.community_value(parent), candidate.get("entry_id"), candidate.get("name", ""), address_match.get("status", "unmatched"), address_match.get("score", 0), address_match.get("method", ""), address_match.get("reason", ""), stable_json(address_match.get("candidates", [])), address_match.get("version", ""), str(parent.get("核查人", "") or "").strip(), identity_hmac, parse_dispatch_time(parent, list(dict.fromkeys([*(list(workflow.date_fields) if workflow else []), "下发日期", "下发时间", "创建时间", "日期"])), first_dispatch_by_key.get(row_key)), projected_task_state, len(source_rows), int(conflict), "\n".join(str(parent.get(column, "") or "") for column in parser.COLUMNS), "", assignment_source_label, assignment_address_display, assignment_address_sort_key, assignment_queue_ready, source_revisions.get(row_key, 0)))
    if address_rows:
        await cur.executemany(
            "INSERT INTO _online_task_address_matches (parser_type,row_key,original_address,suggested_entry_id,suggested_community_id,suggested_community_name,match_status,match_score,match_method,match_reason,candidates_json,matcher_version,confirmed_entry_id,confirmed_by,confirmed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE original_address=VALUES(original_address),suggested_entry_id=VALUES(suggested_entry_id),suggested_community_id=VALUES(suggested_community_id),suggested_community_name=VALUES(suggested_community_name),match_status=VALUES(match_status),match_score=VALUES(match_score),match_method=VALUES(match_method),match_reason=VALUES(match_reason),candidates_json=VALUES(candidates_json),matcher_version=VALUES(matcher_version),confirmed_entry_id=VALUES(confirmed_entry_id),confirmed_by=VALUES(confirmed_by),confirmed_at=VALUES(confirmed_at)", address_rows)
    if projection_rows:
        await cur.executemany(
            "INSERT INTO _online_source_projection (parser_type,row_key,values_json,community,small_community_id,small_community_name,address_match_status,address_match_score,address_match_method,address_match_reason,address_match_candidates,address_match_version,inspector,identity_hmac,first_dispatch_at,task_state,source_count,conflict,search_text,pending_state,assignment_source_label,assignment_address_display,assignment_address_sort_key,assignment_queue_ready,source_revision) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE source_revision=VALUES(source_revision),values_json=VALUES(values_json),community=VALUES(community),small_community_id=VALUES(small_community_id),small_community_name=VALUES(small_community_name),address_match_status=VALUES(address_match_status),address_match_score=VALUES(address_match_score),address_match_method=VALUES(address_match_method),address_match_reason=VALUES(address_match_reason),address_match_candidates=VALUES(address_match_candidates),address_match_version=VALUES(address_match_version),inspector=VALUES(inspector),identity_hmac=VALUES(identity_hmac),first_dispatch_at=COALESCE(_online_source_projection.first_dispatch_at, VALUES(first_dispatch_at)),task_state=VALUES(task_state),source_count=VALUES(source_count),conflict=VALUES(conflict),search_text=VALUES(search_text),pending_state=VALUES(pending_state),assignment_source_label=VALUES(assignment_source_label),assignment_address_display=VALUES(assignment_address_display),assignment_address_sort_key=VALUES(assignment_address_sort_key),assignment_queue_ready=VALUES(assignment_queue_ready)", projection_rows)
    await sync_current_task_snapshots_for_keys(cur, parser_type, keys)
    return {"processed": len(projection_rows), "deleted": len(keys) - len(projection_rows)}


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
                           local_value, status, row_key
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
                        "row_key": str(change[7]),
                    })
                for row in existing:
                    changes = changes_by_source.get(row["id"], [])
                    row["has_local_changes"] = bool(changes)
                    pending_row_keys = {
                        change["row_key"] for change in changes if change["row_key"]
                    }
                    if changes:
                        row["expected_row_key"] = (
                            next(iter(pending_row_keys))
                            if len(pending_row_keys) == 1 else None
                        )

            matched, incoming_only, existing_only = match_source_cache_rows(
                existing, prepared
            )
            # 无法核实两级研判是平台内的独立流程。即使当前没有待写回
            # 字段，腾讯物理行被删除也不能丢掉仍在进行中的本地任务；
            # 保留来源快照（physical_row=-id）后，投影和流程仍可继续，
            # 直到正式结果提交或流程归档。
            active_flow_source_ids: set[int] = set()
            if existing_ids and parser_type in {
                "全链条", "出租房屋核查", "寄递业", "疑似返苏",
                "苏州涉警", "交通涉警",
            }:
                placeholders = ", ".join(["%s"] * len(existing_ids))
                await cur.execute(
                    f"""
                    SELECT source_id
                    FROM _unverifiable_review_flows
                    WHERE parser_type=%s
                      AND source_id IN ({placeholders})
                      AND state IN (
                          'initial_pending','initial_extension','deep_pending',
                          'deep_extension','final_unverifiable','source_exception'
                      )
                    FOR UPDATE
                    """,
                    [parser_type, *existing_ids],
                )
                active_flow_source_ids = {
                    int(row[0]) for row in await cur.fetchall() if row[0] is not None
                }
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
                    await cur.execute(
                        """
                        UPDATE _online_local_changes
                        SET parser_type=%s, spreadsheet_id=%s, sheet_id=%s,
                            physical_row=%s, row_key=%s
                        WHERE id=%s
                        """,
                        (
                            incoming["parser_type"], incoming["spreadsheet_id"],
                            incoming["sheet_id"], incoming["physical_row"],
                            incoming["row_key"], change["id"],
                        ),
                    )
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
                elif old["id"] not in active_flow_source_ids:
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
