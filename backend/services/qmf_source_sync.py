"""后台同步全民防模型三来源，并维护本地任务投影。

模型三不是腾讯在线表：它只从全民防旧平台读取 ``hcjg=未核查``，不调用
任何写接口。来源同步单独作为后台任务运行，已在本地完成的任务会继续保留，
交给全民防反馈核对流程决定何时归档。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from database import db_manager
from services.external_acquisition_jobs import JobContext
from services.online_source import rebuild_projection, source_row_hash
from services.parsers import get_parser
from services.model_three_self_owned import apply_self_owned_matches
from services.qmf_source import (
    MODEL_THREE_PARSER,
    QMF_SOURCE_SHEET_ID,
    QMF_SOURCE_SPREADSHEET_ID,
    fetch_pending_rows,
    resolve_rows,
)
from services.schema_compat import get_database_column_map, quote_identifier


async def _save_qmf_snapshot(conn) -> str | None:
    """保存模型三当天快照，供日报和总汇总使用。

    模型三来源不是腾讯在线表，但它仍然需要和普通来源一样落一份
    业务日期快照；否则总汇总无法判断独立来源是否已经在本轮完成。
    """
    from services.sync_engine import SyncEngine

    return await SyncEngine(None)._save_snapshot(
        conn,
        get_parser(MODEL_THREE_PARSER).table_name,
        MODEL_THREE_PARSER,
    )


def _physical_row(row_key: str, used: set[int]) -> int:
    """为虚拟来源生成稳定的正整数行号，避免来源重排导致实体 ID 变化。"""
    value = int(hashlib.sha256(row_key.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF
    value = value or 1
    while value in used:
        value = value + 1 if value < 0x7FFFFFFF else 1
    used.add(value)
    return value


def _values_from_row(row: tuple, columns: list[str]) -> dict[str, str]:
    return {
        column: str(value or "").strip()
        for column, value in zip(columns, row)
    }


async def _sync_rows(ctx: JobContext, result: dict[str, Any]) -> dict[str, Any]:
    parser = get_parser(MODEL_THREE_PARSER)
    pool = db_manager.get_pool("online_data")
    rows = [parser.normalize_source_row(row) for row in (result.get("rows") or [])]
    await ctx.update(
        phase="写入本地任务",
        current=0,
        total=len(rows),
        message=f"读取 {len(rows)} 条未核查任务，正在更新本地任务池",
    )

    conn = await pool.acquire()
    try:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                column_map = await get_database_column_map(
                    conn, parser.table_name, parser
                )
                quoted = ", ".join(
                    quote_identifier(column_map[column]) for column in parser.COLUMNS
                )
                source_fields = (
                    "截止时间", "核查人", "姓名", "身份证号", "联系方式", "地址", "下发社区"
                )
                update_parts = []
                for column in source_fields:
                    identifier = quote_identifier(column_map[column])
                    if column == "核查人":
                        update_parts.append(
                            f"{identifier}=IF(TRIM(COALESCE({identifier},''))='',VALUES({identifier}),{identifier})"
                        )
                    else:
                        update_parts.append(f"{identifier}=VALUES({identifier})")
                table_update = ", ".join(update_parts)

                # 先把历史上直接写入业务表、但还没有来源缓存的任务迁移到
                # 虚拟全民防来源，保证任务列表和状态核对使用同一投影。
                await cur.execute(
                    "SELECT _row_key," + ",".join(
                        quote_identifier(column_map[column]) for column in parser.COLUMNS
                    ) + f" FROM {parser.table_name}"
                )
                table_rows = {
                    str(row[0]): _values_from_row(row[1:], parser.COLUMNS)
                    for row in await cur.fetchall()
                }
                await cur.execute(
                    "SELECT id,row_key,physical_row,values_json FROM _online_source_rows "
                    "WHERE spreadsheet_id=%s AND parser_type=%s AND sheet_id=%s FOR UPDATE",
                    (QMF_SOURCE_SPREADSHEET_ID, MODEL_THREE_PARSER, QMF_SOURCE_SHEET_ID),
                )
                cache_rows = {}
                used_physical: set[int] = set()
                for source_id, row_key, physical_row, values_json in await cur.fetchall():
                    cache_rows[str(row_key)] = {
                        "id": int(source_id),
                        "physical_row": int(physical_row),
                        "values": values_json,
                    }
                    used_physical.add(int(physical_row))

                for row_key, values in table_rows.items():
                    if row_key in cache_rows:
                        continue
                    physical = _physical_row(row_key, used_physical)
                    await cur.execute(
                        "INSERT INTO _online_source_rows "
                        "(spreadsheet_id,parser_type,sheet_id,physical_row,row_key,row_hash,values_json,cell_meta_json) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            QMF_SOURCE_SPREADSHEET_ID, MODEL_THREE_PARSER, QMF_SOURCE_SHEET_ID,
                            physical, row_key, source_row_hash(values),
                            json.dumps(values, ensure_ascii=False, sort_keys=True),
                            "{}",
                        ),
                    )

                # 来源只提供未核查行；本地核查结果和备注始终由业务表保留。
                for index, incoming in enumerate(rows, 1):
                    row_key = parser.make_row_key(incoming)
                    existing = table_rows.get(row_key, {})
                    merged = dict(existing)
                    merged.update({field: incoming.get(field, "") for field in source_fields})
                    values = [merged.get(column, "") for column in parser.COLUMNS]
                    await cur.execute(
                        f"INSERT INTO {parser.table_name} (_row_key,{quoted}) VALUES (%s,{','.join(['%s'] * len(parser.COLUMNS))}) "
                        f"ON DUPLICATE KEY UPDATE {table_update}",
                        (row_key, *values),
                    )
                    physical = cache_rows.get(row_key, {}).get("physical_row") or _physical_row(row_key, used_physical)
                    cache_payload = {column: merged.get(column, "") for column in parser.COLUMNS}
                    encoded = json.dumps(cache_payload, ensure_ascii=False, sort_keys=True)
                    if row_key in cache_rows:
                        await cur.execute(
                            "UPDATE _online_source_rows SET physical_row=%s,row_hash=%s,values_json=%s,revision=revision+1,refreshed_at=UTC_TIMESTAMP() "
                            "WHERE id=%s",
                            (physical, source_row_hash(cache_payload), encoded, cache_rows[row_key]["id"]),
                        )
                    else:
                        await cur.execute(
                            "INSERT INTO _online_source_rows "
                            "(spreadsheet_id,parser_type,sheet_id,physical_row,row_key,row_hash,values_json,cell_meta_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (QMF_SOURCE_SPREADSHEET_ID, MODEL_THREE_PARSER, QMF_SOURCE_SHEET_ID,
                             physical, row_key, source_row_hash(cache_payload), encoded, "{}"),
                        )
                    table_rows[row_key] = merged
                    await ctx.update(
                        phase="写入本地任务",
                        current=index,
                        total=len(rows),
                        message=f"已处理 {index}/{len(rows)} 条未核查任务",
                    )

                await rebuild_projection(cur, MODEL_THREE_PARSER)
                self_owned_stats = await apply_self_owned_matches(cur)
                if self_owned_stats["updated_tasks"]:
                    await rebuild_projection(cur, MODEL_THREE_PARSER)
                report_date = await _save_qmf_snapshot(conn)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    finally:
        pool.release(conn)

    return {
        "status": "warning" if result.get("unresolved_count") or result.get("issue_count") else "success",
        "message": "全民防未核查任务同步完成",
        "record_count": int(result.get("record_count") or 0),
        "valid_count": len(rows),
        "unresolved_count": int(result.get("unresolved_count") or 0),
        "issue_count": int(result.get("issue_count") or 0),
        "unresolved": result.get("unresolved") or [],
        "self_owned_matched": self_owned_stats["matched_tasks"],
        "self_owned_updated": self_owned_stats["updated_tasks"],
        "report_date": report_date,
    }


async def run_qmf_source_sync(ctx: JobContext) -> dict[str, Any]:
    await ctx.update(phase="读取全民防", current=0, total=None, message="正在读取全民防未核查任务")
    result = await fetch_pending_rows()
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            result = await resolve_rows(cur, result)
    return await _sync_rows(ctx, result)
