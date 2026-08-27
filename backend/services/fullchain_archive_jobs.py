"""全链条导出后的腾讯整行删除后台任务。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from database import db_manager
from routers.query import _oauth_client, _refresh_spreadsheet, _writeback_enabled
from services.online_source import acquire_sheet_lock, resolve_source_columns, source_row_hash
from services.parsers import get_parser
from services.schema_compat import get_database_column_map, quote_identifier
from services.unverifiable_review import FINAL_UNVERIFIABLE, mark_flow_archived, supports_unverifiable_review


_tasks: set[asyncio.Task] = set()

# 腾讯表写回和归档共用同一把 MySQL 命名锁。外部写回可能需要几十秒，
# 归档不能把这段短暂竞争直接记成失败；同时保留上限，避免后台任务无限占用连接。
_ARCHIVE_LOCK_TIMEOUT_SECONDS = 5
_ARCHIVE_LOCK_RETRY_DELAYS = (5, 10, 20, 30)


class _ArchiveLockRetry(Exception):
    """当前归档因腾讯表写回占锁，稍后应重新进入队列。"""


async def _acquire_sheet_lock_with_retry(
    cur,
    spreadsheet_id: int,
    *,
    acquire=acquire_sheet_lock,
    sleep=asyncio.sleep,
    retry_delays: tuple[int, ...] = _ARCHIVE_LOCK_RETRY_DELAYS,
) -> bool:
    """等待腾讯表锁释放，返回是否成功拿到锁。

    ``acquire`` 和 ``sleep`` 可注入，测试无需真实等待，也不会触碰生产锁。
    最后一轮失败由调用方转为 queued/waiting_lock，而不是 error。
    """
    for attempt in range(len(retry_delays) + 1):
        if await acquire(cur, spreadsheet_id, timeout=_ARCHIVE_LOCK_TIMEOUT_SECONDS):
            return True
        if attempt < len(retry_delays):
            await sleep(retry_delays[attempt])
    return False


def _iso(value: Any) -> str | None:
    return value.isoformat() + "Z" if value else None


async def get_archive_export(export_id: int) -> dict[str, Any] | None:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT id,export_no,parser_type,status,phase,file_name,storage_key,file_sha256,
                       total_count,success_count,conflict_count,error_count,
                       categories_json,error_message,requested_by,created_at,
                       started_at,finished_at,updated_at
                FROM _fullchain_archive_exports WHERE id=%s
        """, (export_id,))
            row = await cur.fetchone()
            if not row:
                return None
            await cur.execute("""
                SELECT source_id,category,status,error_code
                       ,external_delete_state,external_deleted_at
                FROM _fullchain_archive_export_items
                WHERE export_id=%s ORDER BY source_id
            """, (export_id,))
            item_rows = await cur.fetchall()
    import json
    return {
        "id": int(row[0]), "export_no": str(row[1]), "parser_type": str(row[2] or "全链条"),
        "status": str(row[3]), "phase": str(row[4]), "file_name": str(row[5]), "storage_key": str(row[6]),
        "file_sha256": str(row[7]), "total_count": int(row[8] or 0),
        "success_count": int(row[9] or 0), "conflict_count": int(row[10] or 0),
        "error_count": int(row[11] or 0), "categories": json.loads(row[12] or "{}"),
        "error_message": str(row[13] or ""), "requested_by": row[14],
        "created_at": _iso(row[15]), "started_at": _iso(row[16]),
        "finished_at": _iso(row[17]), "updated_at": _iso(row[18]),
        "items": [
            {
                "source_id": int(item[0]), "category": str(item[1]),
                "status": str(item[2]), "error_code": str(item[3] or ""),
                "external_delete_state": str(item[4] or "pending"),
                "external_deleted_at": _iso(item[5]),
            }
            for item in item_rows
        ],
    }


async def _mark_item(cur, export_id: int, source_id: int, status: str, error_code: str = "") -> None:
    await cur.execute("""
        UPDATE _fullchain_archive_export_items SET status=%s,error_code=%s
        WHERE export_id=%s AND source_id=%s
    """, (status, error_code[:100], export_id, source_id))


def _safe_error_code(exc: Exception, fallback: str) -> str:
    """只持久化可公开的错误分类，不保存第三方响应或业务正文。"""
    code = str(exc).strip()
    allowed = {
        "online_writeback_disabled",
        "spreadsheet_locked",
        "spreadsheet_unavailable",
        "source_row_changed",
        "platform_archive_failed",
        "external_delete_outcome_unknown",
        "cache_refresh_pending",
    }
    return code if code in allowed else fallback


async def _stage_platform_archive(
    conn,
    parser,
    export_id: int,
    row_key: str,
    values: dict[str, str],
) -> None:
    """在同一事务中写入历史库并移除当前业务表。"""
    table = parser.table_name
    archive_table = f"OnlineDataArchive.{table}_archive"
    column_map = await get_database_column_map(conn, archive_table, parser)
    columns = [quote_identifier(column_map[column]) for column in parser.COLUMNS]
    column_list = ", ".join(columns)
    placeholders = ", ".join(["%s"] * (len(columns) + 2))
    archive_reason = f"fullchain_feedback_export:{export_id}"
    params = [row_key] + [str(values.get(column) or "") for column in parser.COLUMNS] + [archive_reason]
    async with conn.cursor() as cur:
        await cur.execute(
            f"DELETE FROM {archive_table} WHERE _row_key=%s AND _archive_reason=%s",
            (row_key, archive_reason),
        )
        await cur.execute(
            f"INSERT INTO {archive_table} (_row_key,{column_list},_archive_reason) "
            f"VALUES ({placeholders})",
            params,
        )
        await cur.execute(
            f"DELETE FROM {quote_identifier(table)} WHERE _row_key=%s",
            (row_key,),
        )


async def _delete_source_row_once(
    conn,
    client,
    *,
    export_id: int,
    source_id: int,
    parser_type: str,
    spreadsheet: dict[str, Any],
    sheet_id: str,
    physical_row: int,
    expected_revision: int,
    expected_hash: str,
    source_columns: list[str],
    parser,
    external_delete_state: str,
) -> None:
    """腾讯整行删除只允许发送一次，并在发送前后持久化状态。"""
    if external_delete_state == "deleting":
        # 服务可能恰好在外部请求返回前中断。此时无法证明腾讯是否已经删除，
        # 禁止再次按旧物理行发送删除请求，避免伤及已经顶上来的下一行。
        raise RuntimeError("external_delete_outcome_unknown")
    if external_delete_state == "deleted":
        return
    if external_delete_state != "pending":
        raise RuntimeError("external_delete_outcome_unknown")

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT physical_row,revision,row_hash FROM _online_source_rows "
            "WHERE id=%s AND parser_type=%s",
            (source_id, parser_type),
        )
        current_source = await cur.fetchone()
    if (
        not current_source
        or int(current_source[0]) != int(physical_row)
        or int(current_source[1]) != int(expected_revision)
        or str(current_source[2]) != str(expected_hash)
    ):
        raise RuntimeError("source_row_changed")

    live = await client.read_source_row(
        spreadsheet["file_id"], sheet_id, int(physical_row), source_columns
    )
    values = parser.normalize_source_row(live["values"])
    if source_row_hash(values) != str(expected_hash):
        raise RuntimeError("source_row_changed")

    async with conn.cursor() as cur:
        await cur.execute("""
            UPDATE _fullchain_archive_export_items
            SET external_delete_state='deleting',error_code=''
            WHERE export_id=%s AND source_id=%s
              AND external_delete_state='pending'
        """, (export_id, int(source_id)))
        if cur.rowcount != 1:
            raise RuntimeError("external_delete_outcome_unknown")
    # 持久化“请求即将发出”。若进程在请求过程中中断，恢复任务只报告
    # 结果不确定，绝不会按旧物理行再次删除。
    await conn.commit()
    await client.batch_update(
        spreadsheet["file_id"],
        [client.build_delete_row_request(sheet_id, int(physical_row))],
    )
    async with conn.cursor() as cur:
        await cur.execute("""
            UPDATE _fullchain_archive_export_items
            SET external_delete_state='deleted',external_deleted_at=UTC_TIMESTAMP()
            WHERE export_id=%s AND source_id=%s
              AND external_delete_state='deleting'
        """, (export_id, int(source_id)))
        if cur.rowcount != 1:
            # 外部删除已经发送，但确认状态没有安全落库。此时只能暂停并人工
            # 对账，不能继续本地归档，更不能再次发送删除。
            raise RuntimeError("external_delete_outcome_unknown")
    await conn.commit()


async def _commit_platform_archive(
    conn,
    parser,
    *,
    export_id: int,
    source_id: int,
    parser_type: str,
    row_key: str,
    values: dict[str, str],
) -> None:
    """把历史写入、当前表移除、流程归档和条目成功原子提交。"""
    await conn.begin()
    try:
        await _stage_platform_archive(conn, parser, export_id, row_key, values)
        async with conn.cursor() as cur:
            if supports_unverifiable_review(parser_type):
                await mark_flow_archived(cur, parser_type, row_key, export_id)
            await _mark_item(cur, export_id, source_id, "success")
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        raise RuntimeError("platform_archive_failed") from exc


async def run_fullchain_archive_export(export_id: int) -> None:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT parser_type FROM _fullchain_archive_exports WHERE id=%s", (export_id,))
            export_row = await cur.fetchone()
            export_parser_type = str((export_row or ("全链条",))[0] or "全链条")
            await cur.execute("""
                UPDATE _fullchain_archive_exports
                SET status='running',phase='deleting',started_at=COALESCE(started_at,UTC_TIMESTAMP())
                WHERE id=%s AND status IN ('queued','waiting_lock')
            """, (export_id,))
            await cur.execute("""
                SELECT item.source_id,item.spreadsheet_id,item.sheet_id,item.physical_row,
                       item.expected_revision,item.expected_row_hash,
                       COALESCE(source.values_json,item.source_values_json),
                       item.row_key,source.parser_type,spreadsheet.file_id,spreadsheet.data_sheet_id,
                       spreadsheet.header_row,spreadsheet.name,item.external_delete_state
                FROM _fullchain_archive_export_items item
                LEFT JOIN _online_source_rows source ON source.id=item.source_id
                LEFT JOIN _config_spreadsheets spreadsheet ON spreadsheet.id=item.spreadsheet_id
                WHERE item.export_id=%s AND item.status <> 'success'
                ORDER BY item.spreadsheet_id,item.sheet_id,item.physical_row DESC
            """, (export_id,))
            rows = await cur.fetchall()

        groups: dict[tuple[int, str], list[tuple]] = defaultdict(list)
        for row in rows:
            groups[(int(row[1]), str(row[2]))].append(row)
        success = conflicts = errors = 0
        lock_waiting = False
        for (spreadsheet_id, sheet_id), items in groups.items():
            client = None
            locked = False
            try:
                async with conn.cursor() as cur:
                    if not await _writeback_enabled(cur):
                        raise RuntimeError("online_writeback_disabled")
                    if not await _acquire_sheet_lock_with_retry(cur, spreadsheet_id):
                        raise RuntimeError("spreadsheet_locked")
                    locked = True
                    await cur.execute("SELECT id,name,file_id,data_sheet_id,header_row,parser_type FROM _config_spreadsheets WHERE id=%s", (spreadsheet_id,))
                    spreadsheet_row = await cur.fetchone()
                if not spreadsheet_row or not spreadsheet_row[2]:
                    raise RuntimeError("spreadsheet_unavailable")
                if str(spreadsheet_row[5] or "") != export_parser_type:
                    raise RuntimeError("spreadsheet_unavailable")
                spreadsheet = {
                    "id": int(spreadsheet_row[0]), "name": str(spreadsheet_row[1]),
                    "file_id": str(spreadsheet_row[2]), "data_sheet_id": str(spreadsheet_row[3]),
                    "header_row": int(spreadsheet_row[4] or 1), "parser_type": str(spreadsheet_row[5]),
                }
                parser = get_parser(export_parser_type)
                async with conn.cursor() as cur:
                    client = await _oauth_client(cur)
                source_columns = await resolve_source_columns(client, spreadsheet, parser)
                successful_source_ids: list[int] = []
                for row in items:
                    source_id, _, _, physical_row, revision, expected_hash = row[:6]
                    try:
                        delete_state = str(row[13] or "pending")
                        await _delete_source_row_once(
                            conn,
                            client,
                            export_id=export_id,
                            source_id=int(source_id),
                            parser_type=export_parser_type,
                            spreadsheet=spreadsheet,
                            sheet_id=sheet_id,
                            physical_row=int(physical_row),
                            expected_revision=int(revision),
                            expected_hash=str(expected_hash),
                            source_columns=source_columns,
                            parser=parser,
                            external_delete_state=delete_state,
                        )
                        if row[6] is None:
                            raise RuntimeError("platform_archive_failed")
                        import json
                        await _commit_platform_archive(
                            conn,
                            parser,
                            export_id=export_id,
                            source_id=int(source_id),
                            parser_type=export_parser_type,
                            row_key=str(row[7]),
                            values=json.loads(row[6] or "{}"),
                        )
                        successful_source_ids.append(int(source_id))
                        success += 1
                    except Exception as exc:
                        code = _safe_error_code(exc, "tencent_delete_failed")
                        async with conn.cursor() as cur:
                            await _mark_item(cur, export_id, int(source_id), "conflict", code)
                        conflicts += 1
                try:
                    await _refresh_spreadsheet(conn, client, spreadsheet)
                except Exception:
                    # Rows are already deleted; normal sync will retry cache refresh.
                    pass
                if successful_source_ids:
                    placeholders = ",".join(["%s"] * len(successful_source_ids))
                    async with conn.cursor() as cur:
                        await cur.execute(
                            f"SELECT COUNT(*) FROM _online_source_rows WHERE id IN ({placeholders})",
                            successful_source_ids,
                        )
                        remaining = int((await cur.fetchone())[0] or 0)
                    if remaining:
                        # Refresh failure or a later source-position conflict means the
                        # export file remains valid, but the current task cannot yet be
                        # hidden. Mark it for explicit reconciliation.
                        async with conn.cursor() as cur:
                            for source_id in successful_source_ids:
                                await _mark_item(cur, export_id, source_id, "conflict", "cache_refresh_pending")
                        success -= len(successful_source_ids)
                        conflicts += len(successful_source_ids)
            except Exception as exc:
                code = _safe_error_code(exc, "archive_group_failed")
                async with conn.cursor() as cur:
                    for row in items:
                        if code == "spreadsheet_locked":
                            await _mark_item(cur, export_id, int(row[0]), "queued", code)
                        else:
                            await _mark_item(cur, export_id, int(row[0]), "error", code)
                if code == "spreadsheet_locked":
                    lock_waiting = True
                else:
                    errors += len(items)
            finally:
                if client:
                    await client.close()
                if locked:
                    from services.online_source import release_sheet_lock
                    async with conn.cursor() as cur:
                        await release_sheet_lock(cur, spreadsheet_id)

        if lock_waiting:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE _fullchain_archive_exports
                    SET status='queued',phase='waiting_lock',
                        error_message='腾讯表正在写回，归档任务将在锁释放后自动继续'
                    WHERE id=%s
                """, (export_id,))
            raise _ArchiveLockRetry

        # 重试时只读取尚未成功的条目，因此统计必须以数据库内的最终状态为准，
        # 不能沿用本次进程启动时的局部计数。
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT
                    SUM(status='success'), SUM(status='conflict'), SUM(status='error')
                FROM _fullchain_archive_export_items WHERE export_id=%s
            """, (export_id,))
            counts = await cur.fetchone()
            success = int((counts[0] if counts else 0) or 0)
            conflicts = int((counts[1] if counts else 0) or 0)
            errors = int((counts[2] if counts else 0) or 0)
        status = "completed" if conflicts == 0 and errors == 0 else "partial"
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE _fullchain_archive_exports SET status=%s,phase='finished',
                    success_count=%s,conflict_count=%s,error_count=%s,
                    error_message=%s,finished_at=UTC_TIMESTAMP()
                WHERE id=%s
            """, (status, success, conflicts, errors,
                   "部分来源行未删除，请按冲突明细重新核对" if status != "completed" else "", export_id))
    finally:
        pool.release(conn)


def launch_fullchain_archive_export(export_id: int) -> None:
    task = asyncio.create_task(_run_fullchain_archive_export_guarded(export_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run_fullchain_archive_export_guarded(export_id: int) -> None:
    try:
        # 锁竞争只会把任务短暂留在 queued/waiting_lock；后台任务继续退避，
        # 服务重启时 recover_interrupted_fullchain_exports 也会重新接回这类任务。
        for delay in (*_ARCHIVE_LOCK_RETRY_DELAYS, None):
            try:
                await run_fullchain_archive_export(export_id)
                return
            except _ArchiveLockRetry:
                if delay is None:
                    pool = db_manager.get_pool("online_data")
                    async with pool.acquire() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("""
                                UPDATE _fullchain_archive_export_items
                                SET status='error',error_code='spreadsheet_locked'
                                WHERE export_id=%s AND status='queued'
                            """, (export_id,))
                            await cur.execute("""
                                SELECT
                                    SUM(status='success'), SUM(status='conflict'), SUM(status='error')
                                FROM _fullchain_archive_export_items WHERE export_id=%s
                            """, (export_id,))
                            counts = await cur.fetchone()
                            await cur.execute("""
                                UPDATE _fullchain_archive_exports
                                SET status='partial',phase='finished',
                                    success_count=%s,conflict_count=%s,error_count=%s,
                                    error_message='腾讯表持续繁忙，请稍后重新发起归档',
                                    finished_at=UTC_TIMESTAMP()
                                WHERE id=%s AND status IN ('queued','waiting_lock')
                            """, (
                                int((counts[0] if counts else 0) or 0),
                                int((counts[1] if counts else 0) or 0),
                                int((counts[2] if counts else 0) or 0),
                                export_id,
                            ))
                    return
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        raise
    except Exception:
        # 后台异常只保留安全分类，避免第三方响应或业务正文进入运行记录。
        pool = db_manager.get_pool("online_data")
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE _fullchain_archive_exports
                    SET status='failed',phase='finished',error_message='后台归档任务异常，请重新核对',
                        finished_at=UTC_TIMESTAMP()
                    WHERE id=%s AND status IN ('queued','running')
                """, (export_id,))


async def recover_interrupted_fullchain_exports() -> int:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE _fullchain_archive_exports SET status='queued',phase='queued',
                    error_message='服务重启，正在安全恢复归档任务',finished_at=NULL
                WHERE status='running'
            """)
            interrupted = int(cur.rowcount or 0)
            await cur.execute("""
                UPDATE _fullchain_archive_exports export_run
                SET export_run.status='queued',export_run.phase='queued',
                    export_run.error_message='腾讯删除已确认，正在恢复平台归档',
                    export_run.finished_at=NULL
                WHERE export_run.status='partial'
                  AND EXISTS (
                    SELECT 1 FROM _fullchain_archive_export_items item
                    WHERE item.export_id=export_run.id
                      AND item.status<>'success'
                      AND item.external_delete_state='deleted'
                  )
            """)
            interrupted += int(cur.rowcount or 0)
            await cur.execute("""
                SELECT id FROM _fullchain_archive_exports
                WHERE status='queued' AND phase IN ('queued','waiting_lock')
                ORDER BY id
            """)
            queued_ids = [int(row[0]) for row in await cur.fetchall()]
    for export_id in queued_ids:
        launch_fullchain_archive_export(export_id)
    return interrupted


async def stop_fullchain_archive_tasks() -> None:
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
