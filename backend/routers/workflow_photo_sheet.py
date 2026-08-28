"""调照片名单配置、历史导入和双向同步管理接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from deps import require_permission
from database import db_manager
from routers.workflow import get_workflow_db
from services.audit import record_admin_audit, request_audit_fields
from services.permissions import WORKFLOW_CONFIG_MANAGE
from services.photo_sheet_sync import (
    SOURCE_CODE,
    import_online,
    launch_outbox_processing,
    load_source,
    parse_source_url,
    preview_online,
    process_outbox_once,
    sync_online_once,
)
from services.local_source import local_data_source_enabled
from services.external_acquisition_jobs import create_job


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


async def _config_payload(cur, source: dict) -> dict:
    await cur.execute(
        "SELECT "
        "COALESCE(SUM(status IN ('pending','retry')),0),"
        "COALESCE(SUM(status='paused'),0) "
        "FROM photo_sheet_outbox"
    )
    outbox_row = await cur.fetchone() or (0, 0)
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
        "outbox_pending_count": int(outbox_row[0] or 0),
        "outbox_paused_count": int(outbox_row[1] or 0),
    }


@router.get("/config")
async def get_photo_sheet_config(
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    del user
    async with conn.cursor() as cur:
        source = await load_source(cur)
        return await _config_payload(cur, source)


@router.put("/config")
async def update_photo_sheet_config(
    data: PhotoSheetConfigPayload,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    if local_data_source_enabled() and (data.read_enabled or data.write_enabled):
        raise HTTPException(409, "腾讯数据源已下线，不能启用调照片名单同步")
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
        source = await load_source(cur)
        return await _config_payload(cur, source)


@router.post("/preview", status_code=202)
async def preview_photo_sheet(
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    if local_data_source_enabled():
        raise HTTPException(409, "腾讯数据源已下线，不能预览调照片名单")
    async def runner(job):
        pool = db_manager.get_pool("workflow")
        async with pool.acquire() as work_conn:
            async with work_conn.cursor() as cur:
                await job.update(phase="reading", message="正在读取调照片名单")
                result = await preview_online(cur)
        await record_admin_audit(user, "workflow.photo_sheet.preview", target_type="photo_sheet", target_name=SOURCE_CODE, detail={key: result[key] for key in ("rows_read", "requests", "markers", "historical_completed", "pending_after_last_marker", "issue_count", "duplicate_groups", "blocking_issue_count", "warning_count", "excel_date_converted_count", "pending_blocking_count", "pending_warning_count")}, **request_audit_fields(request))
        return {"preview": result, "message": "调照片名单预览完成"}
    job, reused = await create_job("photo_sheet_preview", int(user["id"]), {}, runner, dedupe_key="current")
    return {"run": job, "reused": reused}


@router.post("/import")
async def apply_photo_sheet_import(
    data: ImportPayload,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    if local_data_source_enabled():
        raise HTTPException(409, "腾讯数据源已下线，不能导入调照片名单")
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


@router.post("/sync", status_code=202)
async def run_photo_sheet_sync(
    request: Request,
    full: bool = Query(default=False),
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
):
    if local_data_source_enabled():
        raise HTTPException(409, "腾讯数据源已下线，不能启动调照片同步")
    async def runner(job):
        await job.update(phase="outbox", message="正在处理待写回队列")
        outbox_result = await process_outbox_once()
        await job.update(phase="syncing", message="正在同步腾讯调照片名单")
        sync_result = await sync_online_once(full=full, actor_user_id=int(user["id"]))
        await record_admin_audit(user, "workflow.photo_sheet.sync", target_type="photo_sheet", target_name=SOURCE_CODE, detail={"full": full, "created_tickets": sync_result.get("created_tickets", 0), "completed_tickets": sync_result.get("completed_tickets", 0), "outbox_processed": outbox_result.get("processed", 0), "outbox_failed": outbox_result.get("failed", 0)}, **request_audit_fields(request))
        return {"sync": sync_result, "outbox": outbox_result, "message": "调照片名单同步完成"}
    job, reused = await create_job("photo_sheet_sync", int(user["id"]), {"full": full}, runner, dedupe_key=f"{int(full)}")
    return {"run": job, "reused": reused}


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
            await cur.execute("SELECT COUNT(*) FROM photo_sheet_outbox WHERE status IN ('pending','retry','paused')")
            total = int((await cur.fetchone())[0])
            await cur.execute(
                "SELECT id,work_order_id,action,status,attempt_count,error_code,last_error,updated_at "
                "FROM photo_sheet_outbox WHERE status IN ('pending','retry','paused') "
                "ORDER BY FIELD(status,'paused','retry','pending'),id LIMIT %s OFFSET %s",
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
                "SELECT work_order_id,status,conflict_type FROM photo_sheet_conflicts WHERE id=%s FOR UPDATE",
                (conflict_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "冲突记录不存在")
            if row[1] != "pending":
                raise HTTPException(409, "该冲突已经处理，无需重复重试")
            if str(row[2] or "") == "source_missing":
                raise HTTPException(409, "来源行已不存在，无需重试")
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


@router.post("/outbox/{outbox_id}/retry")
async def retry_photo_sheet_outbox(
    outbox_id: int,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT work_order_id,status,error_code FROM photo_sheet_outbox WHERE id=%s FOR UPDATE",
                (outbox_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "写回任务不存在")
            if row[1] == "done":
                raise HTTPException(409, "该写回任务已经完成")
            if str(row[2] or "") in {"source_missing", "superseded"}:
                raise HTTPException(409, "该任务来源已失效或已合并，无需重试")
            ticket_id = int(row[0])
            await cur.execute(
                "UPDATE photo_sheet_outbox SET status='pending',attempt_count=0,"
                "next_attempt_at=NULL,last_error='',error_code='' WHERE id=%s",
                (outbox_id,),
            )
            await cur.execute(
                "UPDATE photo_request_details SET external_sync_status='pending' "
                "WHERE work_order_id=%s",
                (ticket_id,),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "workflow.photo_sheet.outbox_retry",
        target_type="photo_sheet_outbox",
        target_name=str(outbox_id),
        detail={"action": "manual_retry"},
        **request_audit_fields(request),
    )
    launch_outbox_processing(ticket_id)
    return {"message": "已恢复自动写回", "status": "pending"}
