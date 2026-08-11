"""调照片名单配置、历史导入和双向同步管理接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from deps import require_permission
from routers.workflow import get_workflow_db
from services.audit import record_admin_audit, request_audit_fields
from services.permissions import WORKFLOW_CONFIG_MANAGE
from services.photo_sheet_sync import (
    SOURCE_CODE,
    import_online,
    load_source,
    parse_source_url,
    preview_online,
    process_outbox_once,
    sync_online_once,
)


router = APIRouter(prefix="/api/workflow/photo-sheet", tags=["调照片名单"])


class PhotoSheetConfigPayload(BaseModel):
    file_url: str = Field(default="", max_length=1000)
    header_row: int = Field(default=1, ge=1, le=100)
    read_enabled: bool = False
    write_enabled: bool = False


class ImportPayload(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _config_payload(source: dict) -> dict:
    return {
        "source_code": SOURCE_CODE,
        "file_url": source["file_url"],
        "configured": bool(source["file_id"] and source["sheet_id"]),
        "header_row": source["header_row"],
        "read_enabled": source["read_enabled"],
        "write_enabled": source["write_enabled"],
        "import_applied_at": _iso(source["import_applied_at"]),
        "legacy_cutoff_row": source["legacy_cutoff_row"],
        "last_cursor_row": source["last_cursor_row"],
        "last_full_sync_date": str(source["last_full_sync_date"]) if source["last_full_sync_date"] else None,
        "last_sync_at": _iso(source["last_sync_at"]),
        "last_sync_status": source["last_sync_status"],
        "last_error": source["last_error"],
    }


@router.get("/config")
async def get_photo_sheet_config(
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    del user
    async with conn.cursor() as cur:
        return _config_payload(await load_source(cur))


@router.put("/config")
async def update_photo_sheet_config(
    data: PhotoSheetConfigPayload,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    file_url = data.file_url.strip()
    file_id = sheet_id = ""
    if file_url:
        try:
            file_id, sheet_id = parse_source_url(file_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if (data.read_enabled or data.write_enabled) and not file_id:
        raise HTTPException(422, "启用同步前必须填写有效的腾讯表格地址")
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            source = await load_source(cur, for_update=True)
            target_changed = bool(
                source["file_id"] and (source["file_id"] != file_id or source["sheet_id"] != sheet_id)
            )
            if target_changed and source["import_applied_at"]:
                raise HTTPException(409, "历史导入完成后不能直接更换腾讯表格，请先停用并制定迁移方案")
            await cur.execute(
                "UPDATE photo_sheet_sources SET file_url=%s,file_id=%s,sheet_id=%s,header_row=%s,"
                "read_enabled=%s,write_enabled=%s,last_sync_status=%s,last_error='',updated_by=%s WHERE id=%s",
                (file_url, file_id, sheet_id, data.header_row, int(data.read_enabled), int(data.write_enabled),
                 "idle" if data.read_enabled or data.write_enabled else "disabled", user["id"], source["id"]),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "workflow.photo_sheet.config", target_type="photo_sheet", target_name=SOURCE_CODE,
        detail={"configured": bool(file_id), "header_row": data.header_row,
                "read_enabled": data.read_enabled, "write_enabled": data.write_enabled},
        **request_audit_fields(request),
    )
    async with conn.cursor() as cur:
        return _config_payload(await load_source(cur))


@router.post("/preview")
async def preview_photo_sheet(
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    try:
        async with conn.cursor() as cur:
            result = await preview_online(cur)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    await record_admin_audit(
        user, "workflow.photo_sheet.preview", target_type="photo_sheet", target_name=SOURCE_CODE,
        detail={key: result[key] for key in (
            "rows_read", "requests", "markers", "historical_completed",
            "pending_after_last_marker", "issue_count", "duplicate_groups",
            "blocking_issue_count", "warning_count", "excel_date_converted_count",
            "pending_blocking_count", "pending_warning_count",
        )}, **request_audit_fields(request),
    )
    return result


@router.post("/import")
async def apply_photo_sheet_import(
    data: ImportPayload,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            result = await import_online(cur, expected_token=data.preview_token, actor_user_id=int(user["id"]))
        await conn.commit()
    except ValueError as exc:
        await conn.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        await conn.rollback()
        raise HTTPException(502, str(exc)) from exc
    await record_admin_audit(
        user, "workflow.photo_sheet.import", target_type="photo_sheet", target_name=SOURCE_CODE,
        detail={key: result.get(key) for key in (
            "requests", "markers", "historical_completed", "pending_after_last_marker",
            "issue_count", "blocking_issue_count", "warning_count",
            "excel_date_converted_count", "created_tickets",
        )}, **request_audit_fields(request),
    )
    return result


@router.post("/sync")
async def run_photo_sheet_sync(
    request: Request,
    full: bool = Query(default=False),
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
):
    try:
        sync_result = await sync_online_once(full=full, actor_user_id=int(user["id"]))
        outbox_result = await process_outbox_once()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    await record_admin_audit(
        user, "workflow.photo_sheet.sync", target_type="photo_sheet", target_name=SOURCE_CODE,
        detail={
            "full": full,
            "created_tickets": sync_result.get("created_tickets", 0),
            "completed_tickets": sync_result.get("completed_tickets", 0),
            "outbox_processed": outbox_result.get("processed", 0),
            "outbox_failed": outbox_result.get("failed", 0),
        }, **request_audit_fields(request),
    )
    return {"sync": sync_result, "outbox": outbox_result}


@router.get("/runs")
async def list_photo_sheet_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM photo_sheet_sync_runs")
        total = int((await cur.fetchone())[0])
        await cur.execute(
            "SELECT id,run_type,status,rows_read,requests_found,markers_found,created_tickets,completed_tickets,"
            "issue_count,error_message,started_at,finished_at FROM photo_sheet_sync_runs "
            "ORDER BY id DESC LIMIT %s OFFSET %s", (page_size, (page - 1) * page_size),
        )
        rows = await cur.fetchall()
    return {"total": total, "page": page, "page_size": page_size, "data": [
        {"id": int(row[0]), "run_type": row[1], "status": row[2], "rows_read": int(row[3]),
         "requests_found": int(row[4]), "markers_found": int(row[5]), "created_tickets": int(row[6]),
         "completed_tickets": int(row[7]), "issue_count": int(row[8]), "error_message": row[9],
         "started_at": _iso(row[10]), "finished_at": _iso(row[11])} for row in rows
    ]}


@router.get("/issues")
async def list_photo_sheet_issues(
    kind: str = Query(default="data", pattern="^(data|requester|conflict|outbox)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    del user
    offset = (page - 1) * page_size
    async with conn.cursor() as cur:
        if kind in {"data", "requester"}:
            condition = "detail.data_issue<>''"
            if kind == "requester":
                condition = "detail.data_issue LIKE '%%申请人%%'"
            await cur.execute(
                f"SELECT COUNT(*) FROM photo_request_details detail WHERE detail.external_origin='tencent' AND {condition}"
            )
            total = int((await cur.fetchone())[0])
            await cur.execute(
                f"SELECT detail.work_order_id,map.physical_row,detail.data_issue FROM photo_request_details detail "
                f"LEFT JOIN photo_sheet_rows map ON map.work_order_id=detail.work_order_id "
                f"WHERE detail.external_origin='tencent' AND {condition} ORDER BY map.physical_row LIMIT %s OFFSET %s",
                (page_size, offset),
            )
            data = [{"work_order_id": int(row[0]), "physical_row": row[1], "safe_detail": row[2]} for row in await cur.fetchall()]
        elif kind == "conflict":
            await cur.execute("SELECT COUNT(*) FROM photo_sheet_conflicts WHERE status='pending'")
            total = int((await cur.fetchone())[0])
            await cur.execute(
                "SELECT id,work_order_id,physical_row,conflict_type,safe_detail,created_at FROM photo_sheet_conflicts "
                "WHERE status='pending' ORDER BY id DESC LIMIT %s OFFSET %s", (page_size, offset),
            )
            data = [{"id": int(row[0]), "work_order_id": row[1], "physical_row": row[2],
                     "type": row[3], "safe_detail": row[4], "created_at": _iso(row[5])} for row in await cur.fetchall()]
        else:
            await cur.execute("SELECT COUNT(*) FROM photo_sheet_outbox WHERE status IN ('pending','retry')")
            total = int((await cur.fetchone())[0])
            await cur.execute(
                "SELECT id,work_order_id,action,status,attempt_count,error_code,last_error,updated_at "
                "FROM photo_sheet_outbox WHERE status IN ('pending','retry') ORDER BY id LIMIT %s OFFSET %s",
                (page_size, offset),
            )
            data = [{"id": int(row[0]), "work_order_id": int(row[1]), "action": row[2], "status": row[3],
                     "attempt_count": int(row[4]), "error_code": row[5], "safe_detail": row[6],
                     "updated_at": _iso(row[7])} for row in await cur.fetchall()]
    return {"total": total, "page": page, "page_size": page_size, "data": data}


@router.post("/conflicts/{conflict_id}/retry")
async def retry_photo_sheet_conflict(
    conflict_id: int,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT work_order_id,status FROM photo_sheet_conflicts WHERE id=%s FOR UPDATE", (conflict_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "冲突记录不存在")
            await cur.execute(
                "UPDATE photo_sheet_outbox SET status='pending',next_attempt_at=NULL,last_error='',error_code='' "
                "WHERE work_order_id=%s AND status<>'done'", (row[0],),
            )
            await cur.execute(
                "UPDATE photo_sheet_conflicts SET status='retrying',resolved_by=%s,resolved_at=UTC_TIMESTAMP() WHERE id=%s",
                (user["id"], conflict_id),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "workflow.photo_sheet.conflict_retry", target_type="photo_sheet_conflict",
        target_name=str(conflict_id), detail={}, **request_audit_fields(request),
    )
    return {"message": "已重新加入安全定位队列"}
