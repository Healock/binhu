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
        "SELECT row_key, values_json FROM _online_source_rows "
        "WHERE parser_type=%s ORDER BY spreadsheet_id, physical_row",
        (parser_type,),
    )
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row_key, raw_values in await cur.fetchall():
        values = json_value(raw_values, {})
        grouped[str(row_key)].append({
            column: str(values.get(column, "") or "").strip()
            for column in parser.COLUMNS
        })

    await cur.execute(
        "SELECT row_key_before, row_key_after FROM _online_writeback_audit "
        "WHERE parser_type=%s AND sync_status='pending'",
        (parser_type,),
    )
    pending_keys = {
        str(value)
        for row in await cur.fetchall()
        for value in row
        if value
    }
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
        pending_keys.update(str(row[0]) for row in await cur.fetchall() if row[0])

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
            "pending" if row_key in pending_keys else "",
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
    """用一次腾讯读取结果原子替换来源定位并重建业务投影。"""
    parser = get_parser(spreadsheet["parser_type"])
    prepared = []
    for source in source_rows:
        values = parser.normalize_source_row(source.get("values", {}))
        metadata = {
            column: (source.get("cell_meta") or {}).get(column, {"type": "text"})
            for column in parser.COLUMNS
        }
        prepared.append((
            int(spreadsheet["id"]),
            spreadsheet["parser_type"],
            str(spreadsheet["data_sheet_id"]),
            int(source["physical_row"]),
            parser.make_row_key(values),
            source_row_hash(values),
            stable_json(values),
            stable_json(metadata),
        ))

    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM _online_source_rows WHERE spreadsheet_id=%s",
                (spreadsheet["id"],),
            )
            if prepared:
                await cur.executemany(
                    """
                    INSERT INTO _online_source_rows (
                        spreadsheet_id, parser_type, sheet_id, physical_row,
                        row_key, row_hash, values_json, cell_meta_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    prepared,
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
