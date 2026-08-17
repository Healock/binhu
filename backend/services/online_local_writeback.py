"""流口任务本地优先保存和腾讯字段级异步写回。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from database import db_manager
from services.online_source import (
    acquire_sheet_lock,
    json_value,
    rebuild_projection,
    release_sheet_lock,
    resolve_source_columns,
    source_row_hash,
    stable_json,
)
from services.parsers import get_parser
from services.task_workflow import TASK_WORKFLOWS
from services.txdocs_client import TxDocsAPIError, TxDocsClient


ACTIVE_STATUSES = {"pending", "processing", "retry", "conflict"}
PROCESSABLE_STATUSES = {"pending", "retry"}
_PROCESS_LOCK = asyncio.Lock()
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def writeback_cell_metadata(
    parser_type: str,
    field: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """补齐腾讯空白结果下拉缺失的业务选项。"""
    prepared = dict(metadata or {"type": "text"})
    workflow = TASK_WORKFLOWS.get(parser_type)
    cell_type = str(prepared.get("write_type") or prepared.get("type") or "text")
    if not workflow or field != workflow.result_field or cell_type != "select":
        return prepared

    options: list[dict[str, Any]] = []
    known_texts: set[str] = set()
    for option in prepared.get("write_options") or prepared.get("options") or []:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id") or "").strip()
        text = str(option.get("text") or "").strip()
        if not option_id or not text or text in known_texts:
            continue
        normalized = dict(option)
        normalized["id"] = option_id
        normalized["text"] = text
        options.append(normalized)
        known_texts.add(text)

    for text in workflow.result_options:
        if text not in known_texts:
            options.append({"id": text, "text": text})
            known_texts.add(text)

    prepared["write_options"] = options
    return prepared


def _retry_error_details(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ValueError):
        return "write_validation_failed", "腾讯写回参数校验未通过"
    if isinstance(exc, TxDocsAPIError):
        return "txdocs_api_failed", "腾讯接口暂未完成写回"
    return "write_failed", f"腾讯写回暂未完成：{type(exc).__name__}"


def overlay_local_values(
    values: dict[str, str],
    changes: list[dict[str, Any]],
) -> dict[str, str]:
    effective = {key: str(value or "") for key, value in values.items()}
    for change in changes:
        if str(change.get("status") or "") in ACTIVE_STATUSES:
            effective[str(change.get("field_name") or "")] = str(
                change.get("local_value") or ""
            )
    return effective


def local_sync_state(changes: list[dict[str, Any]]) -> str:
    statuses = {str(change.get("status") or "") for change in changes}
    if "conflict" in statuses:
        return "conflict"
    if "retry" in statuses:
        return "retry"
    if statuses & {"pending", "processing"}:
        return "pending"
    return ""


def split_remote_changes(
    remote_values: dict[str, str],
    changes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    safe: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for change in changes:
        field = str(change["field_name"])
        remote = str(remote_values.get(field, "") or "")
        base = str(change.get("base_value") or "")
        local = str(change.get("local_value") or "")
        item = {**change, "remote_value": remote}
        if remote == local or remote == base:
            safe.append(item)
        else:
            conflicts.append(item)
    return safe, conflicts


async def load_local_changes(
    cur,
    source_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    if not source_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(source_ids))
    await cur.execute(
        f"""
        SELECT source_id, id, audit_id, field_name, base_value, local_value,
               remote_value, status, error_code, last_error
        FROM _online_local_changes
        WHERE source_id IN ({placeholders})
          AND status IN ('pending','processing','retry','conflict')
        ORDER BY id
        """,
        source_ids,
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in await cur.fetchall():
        grouped.setdefault(int(row[0]), []).append({
            "id": int(row[1]),
            "audit_id": int(row[2]),
            "field_name": str(row[3]),
            "base_value": str(row[4] or ""),
            "local_value": str(row[5] or ""),
            "remote_value": None if row[6] is None else str(row[6]),
            "status": str(row[7]),
            "error_code": str(row[8] or ""),
            "last_error": str(row[9] or ""),
        })
    return grouped


async def _refresh_audit_status(cur, audit_ids: set[int]) -> None:
    for audit_id in audit_ids:
        await cur.execute(
            "SELECT status FROM _online_local_changes WHERE audit_id=%s",
            (audit_id,),
        )
        statuses = {str(row[0]) for row in await cur.fetchall()}
        if "conflict" in statuses:
            status = "conflict"
        elif statuses & {"pending", "processing", "retry"}:
            status = "pending"
        else:
            status = "superseded"
        await cur.execute(
            "UPDATE _online_writeback_audit SET sync_status=%s, "
            "synced_at=IF(%s='synced',UTC_TIMESTAMP(),synced_at) WHERE id=%s",
            (status, status, audit_id),
        )


async def enqueue_local_changes(
    conn,
    *,
    source: dict,
    changes: dict[str, str],
    user: dict,
    audit_id: int,
) -> int:
    source_id = int(source["id"])
    affected_audits = {int(audit_id)}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT revision, values_json FROM _online_source_rows "
            "WHERE id=%s AND parser_type=%s FOR UPDATE",
            (source_id, source["spreadsheet"]["parser_type"]),
        )
        locked = await cur.fetchone()
        if not locked:
            raise LookupError("来源行已经变化")
        remote_values = json_value(locked[1], {})
        fields = list(changes)
        placeholders = ", ".join(["%s"] * len(fields))
        await cur.execute(
            f"SELECT field_name, base_value, audit_id, status "
            f"FROM _online_local_changes WHERE source_id=%s "
            f"AND field_name IN ({placeholders}) FOR UPDATE",
            [source_id, *fields],
        )
        existing = {
            str(row[0]): {
                "base_value": str(row[1] or ""),
                "audit_id": int(row[2]),
                "status": str(row[3]),
            }
            for row in await cur.fetchall()
        }
        for field, local_value in changes.items():
            previous = existing.get(field)
            base_value = (
                previous["base_value"]
                if previous else str(remote_values.get(field, "") or "")
            )
            if previous:
                affected_audits.add(previous["audit_id"])
            if str(local_value or "") == base_value:
                await cur.execute(
                    "DELETE FROM _online_local_changes "
                    "WHERE source_id=%s AND field_name=%s",
                    (source_id, field),
                )
                continue
            next_status = (
                "conflict"
                if previous and previous["status"] == "conflict"
                else "pending"
            )
            await cur.execute(
                """
                INSERT INTO _online_local_changes (
                    audit_id, source_id, parser_type, spreadsheet_id,
                    sheet_id, physical_row, row_key, field_name,
                    base_value, local_value, status, user_id, username
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    audit_id=VALUES(audit_id),
                    parser_type=VALUES(parser_type),
                    spreadsheet_id=VALUES(spreadsheet_id),
                    sheet_id=VALUES(sheet_id),
                    physical_row=VALUES(physical_row),
                    row_key=VALUES(row_key),
                    local_value=VALUES(local_value),
                    status=VALUES(status),
                    attempt_count=0,
                    next_attempt_at=NULL,
                    error_code='',
                    last_error='',
                    user_id=VALUES(user_id),
                    username=VALUES(username)
                """,
                (
                    audit_id,
                    source_id,
                    source["spreadsheet"]["parser_type"],
                    source["spreadsheet_id"],
                    source["sheet_id"],
                    source["physical_row"],
                    source["row_key"],
                    field,
                    base_value,
                    str(local_value or ""),
                    next_status,
                    int(user["id"]),
                    str(user.get("username") or "")[:50],
                ),
            )
        await cur.execute(
            "UPDATE _online_source_rows SET revision=revision+1, "
            "refreshed_at=UTC_TIMESTAMP() WHERE id=%s",
            (source_id,),
        )
        await cur.execute(
            "SELECT revision FROM _online_source_rows WHERE id=%s",
            (source_id,),
        )
        revision = int((await cur.fetchone())[0])
        await _refresh_audit_status(cur, affected_audits)
        await rebuild_projection(cur, source["spreadsheet"]["parser_type"])
    return revision


async def source_sync_payload(cur, source_id: int) -> dict[str, Any]:
    grouped = await load_local_changes(cur, [source_id])
    changes = grouped.get(source_id, [])
    return {
        "state": local_sync_state(changes),
        "fields": [
            {
                "field": item["field_name"],
                "platform_value": item["local_value"],
                "tencent_value": item["remote_value"],
                "status": item["status"],
                "error_code": item["error_code"],
            }
            for item in changes
        ],
    }


async def resolve_source_conflict(
    conn,
    *,
    source_id: int,
    choice: str,
    fields: list[str],
) -> dict[str, Any]:
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT parser_type, physical_row FROM _online_source_rows "
                "WHERE id=%s FOR UPDATE",
                (source_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise LookupError("来源行已经变化")
            parser_type = str(row[0])
            physical_row = int(row[1])
            params: list[Any] = [source_id]
            field_clause = ""
            if fields:
                placeholders = ", ".join(["%s"] * len(fields))
                field_clause = f" AND field_name IN ({placeholders})"
                params.extend(fields)
            await cur.execute(
                "SELECT id, audit_id, remote_value, error_code, field_name "
                "FROM _online_local_changes "
                "WHERE source_id=%s AND status='conflict'" + field_clause + " FOR UPDATE",
                params,
            )
            conflicts = await cur.fetchall()
            if not conflicts:
                raise ValueError("没有可处理的同步冲突")
            audit_ids = {int(item[1]) for item in conflicts}
            ids = [int(item[0]) for item in conflicts]
            placeholders = ", ".join(["%s"] * len(ids))
            if choice == "platform":
                if any(str(item[3] or "") == "source_missing" for item in conflicts):
                    raise ValueError("腾讯来源行已不存在，不能再用平台值覆盖")
                await cur.execute(
                    f"UPDATE _online_local_changes SET base_value=COALESCE(remote_value,''), "
                    f"status='pending', attempt_count=0, next_attempt_at=NULL, "
                    f"error_code='', last_error='' WHERE id IN ({placeholders})",
                    ids,
                )
            elif choice == "tencent":
                await cur.execute(
                    f"DELETE FROM _online_local_changes WHERE id IN ({placeholders})",
                    ids,
                )
            else:
                raise ValueError("未知冲突处理方式")
            await cur.execute(
                "UPDATE _online_source_rows SET revision=revision+1, "
                "refreshed_at=UTC_TIMESTAMP() WHERE id=%s",
                (source_id,),
            )
            if choice == "tencent" and physical_row < 0:
                await cur.execute(
                    "SELECT 1 FROM _online_local_changes WHERE source_id=%s "
                    "AND status IN ('pending','processing','retry','conflict') LIMIT 1",
                    (source_id,),
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "DELETE FROM _online_source_rows WHERE id=%s",
                        (source_id,),
                    )
            await _refresh_audit_status(cur, audit_ids)
            await rebuild_projection(cur, parser_type)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {
        "choice": choice,
        "fields": [str(item[4]) for item in conflicts],
        "sync_state": "pending" if choice == "platform" else "",
    }


async def _oauth_client(cur) -> TxDocsClient:
    await cur.execute(
        "SELECT client_id, access_token, open_id "
        "FROM _config_oauth_tokens ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if not row or not row[1] or not row[2]:
        raise RuntimeError("腾讯文档 OAuth 尚未配置")
    return TxDocsClient(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        usage_source="local_writeback",
    )


async def _writeback_enabled(cur) -> bool:
    await cur.execute(
        "SELECT config_value FROM _system_config "
        "WHERE config_key='online_writeback_enabled'"
    )
    row = await cur.fetchone()
    return bool(row and str(row[0]) == "1")


async def _mark_source_retry(conn, source_id: int, exc: Exception) -> None:
    error_code, message = _retry_error_details(exc)
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _online_local_changes SET status='retry', "
                "attempt_count=attempt_count+1, "
                "next_attempt_at=DATE_ADD(UTC_TIMESTAMP(),INTERVAL 5 MINUTE), "
                "error_code=%s, last_error=%s "
                "WHERE source_id=%s AND status IN ('pending','processing','retry')",
                (error_code, message[:500], source_id),
            )
            await cur.execute(
                "SELECT DISTINCT audit_id FROM _online_local_changes WHERE source_id=%s",
                (source_id,),
            )
            await _refresh_audit_status(
                cur, {int(row[0]) for row in await cur.fetchall()}
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def _process_source(conn, source_id: int) -> tuple[int, int]:
    client = None
    spreadsheet_id = None
    try:
        async with conn.cursor() as cur:
            if not await _writeback_enabled(cur):
                return 0, 0
            await cur.execute(
                """
                SELECT source.id, source.parser_type, source.spreadsheet_id,
                       source.sheet_id, source.physical_row, source.row_key,
                       spreadsheet.file_id, spreadsheet.data_sheet_id,
                       spreadsheet.header_row, spreadsheet.enabled
                FROM _online_source_rows AS source
                JOIN _config_spreadsheets AS spreadsheet
                  ON spreadsheet.id=source.spreadsheet_id
                WHERE source.id=%s
                """,
                (source_id,),
            )
            row = await cur.fetchone()
            if not row or not row[9]:
                parser_type = str(row[1]) if row else ""
                if not parser_type:
                    await cur.execute(
                        "SELECT parser_type FROM _online_local_changes "
                        "WHERE source_id=%s LIMIT 1",
                        (source_id,),
                    )
                    local_row = await cur.fetchone()
                    parser_type = str(local_row[0]) if local_row else ""
                await cur.execute(
                    "UPDATE _online_local_changes SET status='conflict', "
                    "error_code='source_missing', last_error='腾讯来源行已不存在' "
                    "WHERE source_id=%s AND status IN ('pending','retry','processing')",
                    (source_id,),
                )
                await cur.execute(
                    "SELECT DISTINCT audit_id FROM _online_local_changes "
                    "WHERE source_id=%s",
                    (source_id,),
                )
                await _refresh_audit_status(
                    cur, {int(item[0]) for item in await cur.fetchall()}
                )
                if parser_type:
                    await rebuild_projection(cur, parser_type)
                await conn.commit()
                return 0, 1
            source = {
                "id": int(row[0]),
                "parser_type": str(row[1]),
                "spreadsheet_id": int(row[2]),
                "sheet_id": str(row[3]),
                "physical_row": int(row[4]),
                "row_key": str(row[5]),
                "file_id": str(row[6]),
                "data_sheet_id": str(row[7]),
                "header_row": int(row[8] or 1),
            }
            spreadsheet_id = source["spreadsheet_id"]
            if not await acquire_sheet_lock(cur, spreadsheet_id, timeout=2):
                return 0, 0
            client = await _oauth_client(cur)
            await cur.execute(
                "UPDATE _online_local_changes SET status='processing' "
                "WHERE source_id=%s AND status IN ('pending','retry') "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=UTC_TIMESTAMP())",
                (source_id,),
            )
            await conn.commit()

        parser = get_parser(source["parser_type"])
        spreadsheet = {
            "id": spreadsheet_id,
            "parser_type": source["parser_type"],
            "file_id": source["file_id"],
            "data_sheet_id": source["data_sheet_id"],
            "header_row": source["header_row"],
        }
        columns = await resolve_source_columns(client, spreadsheet, parser)
        raw = await client.read_source_row(
            source["file_id"], source["sheet_id"], source["physical_row"], columns
        )
        remote_values = parser.normalize_source_row(raw["values"])
        async with conn.cursor() as cur:
            grouped = await load_local_changes(cur, [source_id])
        processing = [
            item for item in grouped.get(source_id, [])
            if item["status"] == "processing"
        ]
        if not processing:
            return 0, 0
        safe, conflicts = split_remote_changes(remote_values, processing)
        if any(
            item["remote_value"] != item["local_value"] for item in safe
        ):
            raw = await client.read_source_row(
                source["file_id"], source["sheet_id"], source["physical_row"], columns
            )
            remote_values = parser.normalize_source_row(raw["values"])
            safe, conflicts = split_remote_changes(remote_values, processing)
        requests = []
        for change in safe:
            field = change["field_name"]
            if change["remote_value"] == change["local_value"]:
                continue
            metadata = writeback_cell_metadata(
                source["parser_type"],
                field,
                (raw.get("cell_meta") or {}).get(field),
            )
            requests.append(client.build_update_cell_request(
                source["sheet_id"],
                source["physical_row"],
                columns.index(field),
                change["local_value"],
                metadata,
                field,
            ))
        if requests:
            await client.batch_update(source["file_id"], requests)
            raw = await client.read_source_row(
                source["file_id"], source["sheet_id"], source["physical_row"], columns
            )
            remote_values = parser.normalize_source_row(raw["values"])
        verified_safe = [
            item for item in safe
            if str(remote_values.get(item["field_name"], "") or "")
            == item["local_value"]
        ]
        failed_safe = [item for item in safe if item not in verified_safe]

        await conn.begin()
        try:
            async with conn.cursor() as cur:
                successful_audits: set[int] = set()
                superseded_audits: set[int] = set()
                verified_count = 0
                for item in verified_safe:
                    await cur.execute(
                        "DELETE FROM _online_local_changes "
                        "WHERE id=%s AND audit_id=%s AND status='processing' "
                        "AND local_value=%s",
                        (item["id"], item["audit_id"], item["local_value"]),
                    )
                    if cur.rowcount == 1:
                        verified_count += 1
                        successful_audits.add(int(item["audit_id"]))
                    else:
                        superseded_audits.add(int(item["audit_id"]))
                conflict_items = [*conflicts, *failed_safe]
                conflict_count = 0
                for item in conflict_items:
                    await cur.execute(
                        "UPDATE _online_local_changes SET status='conflict', "
                        "remote_value=%s, error_code=%s, last_error=%s "
                        "WHERE id=%s AND audit_id=%s AND status='processing' "
                        "AND local_value=%s",
                        (
                            str(remote_values.get(item["field_name"], "") or ""),
                            "field_changed" if item in conflicts else "verify_failed",
                            "腾讯同一字段已被修改" if item in conflicts else "腾讯写后回读不一致",
                            item["id"],
                            item["audit_id"],
                            item["local_value"],
                        ),
                    )
                    if cur.rowcount == 1:
                        conflict_count += 1
                    else:
                        superseded_audits.add(int(item["audit_id"]))
                metadata = {
                    column: (raw.get("cell_meta") or {}).get(column, {"type": "text"})
                    for column in parser.COLUMNS
                }
                await cur.execute(
                    """
                    UPDATE _online_source_rows
                    SET row_key=%s, row_hash=%s, values_json=%s,
                        cell_meta_json=%s, revision=revision+1,
                        refreshed_at=UTC_TIMESTAMP()
                    WHERE id=%s
                    """,
                    (
                        parser.make_row_key(remote_values),
                        source_row_hash(remote_values),
                        stable_json(remote_values),
                        stable_json(metadata),
                        source_id,
                    ),
                )
                audit_ids = {
                    int(item["audit_id"])
                    for item in processing
                }
                for audit_id in audit_ids:
                    await cur.execute(
                        "SELECT status FROM _online_local_changes WHERE audit_id=%s",
                        (audit_id,),
                    )
                    statuses = {str(row[0]) for row in await cur.fetchall()}
                    if "conflict" in statuses:
                        status = "conflict"
                    elif statuses & {"pending", "processing", "retry"}:
                        status = "pending"
                    elif audit_id in superseded_audits:
                        status = "superseded"
                    elif audit_id in successful_audits:
                        status = "synced"
                    else:
                        status = "superseded"
                    await cur.execute(
                        "UPDATE _online_writeback_audit SET sync_status=%s, "
                        "synced_at=IF(%s='synced',UTC_TIMESTAMP(),synced_at) "
                        "WHERE id=%s",
                        (status, status, audit_id),
                    )
                await rebuild_projection(cur, source["parser_type"])
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        return verified_count, conflict_count
    except asyncio.CancelledError:
        with suppress(Exception):
            await _mark_source_retry(conn, source_id, RuntimeError("write_cancelled"))
        raise
    except (TxDocsAPIError, Exception) as exc:
        with suppress(Exception):
            await _mark_source_retry(conn, source_id, exc)
        return 0, 1
    finally:
        if client:
            await client.close()
        if spreadsheet_id is not None:
            with suppress(Exception):
                async with conn.cursor() as cur:
                    await release_sheet_lock(cur, spreadsheet_id)


async def process_local_changes_once(
    limit: int = 20,
    source_id: int | None = None,
) -> dict[str, int]:
    async with _PROCESS_LOCK:
        pool = db_manager.get_pool("online_data")
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                source_clause = " AND source_id=%s" if source_id is not None else ""
                params: list[Any] = [source_id] if source_id is not None else []
                params.append(limit)
                await cur.execute(
                    "SELECT DISTINCT source_id FROM _online_local_changes "
                    "WHERE status IN ('pending','retry') "
                    "AND (next_attempt_at IS NULL OR next_attempt_at<=UTC_TIMESTAMP())"
                    + source_clause + " ORDER BY source_id LIMIT %s",
                    params,
                )
                source_ids = [int(row[0]) for row in await cur.fetchall()]
            synced = conflicts = 0
            for pending_source_id in source_ids:
                source_synced, source_conflicts = await _process_source(
                    conn, pending_source_id
                )
                synced += source_synced
                conflicts += source_conflicts
            return {"processed": len(source_ids), "synced": synced, "conflicts": conflicts}


async def _process_source_background(source_id: int) -> None:
    try:
        await asyncio.wait_for(
            process_local_changes_once(limit=1, source_id=source_id),
            timeout=45,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(
            "[ONLINE_WRITEBACK] immediate processing failed: "
            f"source={source_id} error={type(exc).__name__}"
        )


def launch_local_change_processing(source_id: int) -> None:
    task = asyncio.create_task(_process_source_background(source_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def run_online_writeback_scheduler() -> None:
    while True:
        try:
            await process_local_changes_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[ONLINE_WRITEBACK] scheduler failed: {type(exc).__name__}")
        await asyncio.sleep(5)


async def stop_online_writeback_tasks() -> None:
    tasks = list(_BACKGROUND_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
