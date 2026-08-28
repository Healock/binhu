"""数据同步、定时配置和状态 API。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from database import get_db
from config import settings
from deps import get_current_user, require_permission, require_super_admin
from schemas.sync import SyncStatusResponse, SyncTriggerResponse
from services.permissions import ONLINE_SUMMARY_VIEW, SYNC_TRIGGER
from services.sync_tasks import (
    create_sync_task,
    get_schedule,
    update_schedule,
)
from services.audit import record_admin_audit, request_audit_fields

require_admin = require_permission(SYNC_TRIGGER)

router = APIRouter(prefix="/api/sync", tags=["数据同步"])


class SyncScheduleRequest(BaseModel):
    enabled: bool
    interval_minutes: int = Field(ge=5, le=10080)


def _iso_utc(value) -> str | None:
    return value.isoformat() + "Z" if value else None


@router.post("/trigger", response_model=SyncTriggerResponse)
async def trigger_sync(
    request: Request,
    user: dict = Depends(require_admin),
):
    """由管理员或超级管理员触发全量同步。"""
    if settings.LOCAL_DATA_SOURCE_ENABLED and not settings.TXDOCS_ENABLED:
        return SyncTriggerResponse(
            task_id=0,
            status="disabled",
            message="腾讯数据源已下线，当前业务数据由本地任务池提供",
        )
    task_id, status, message = await create_sync_task(
        "manual",
        requested_by=user["id"],
    )
    await record_admin_audit(
        user,
        "sync.trigger",
        target_type="sync",
        target_name=str(task_id) if task_id else "",
        result=status,
        **request_audit_fields(request),
    )
    return SyncTriggerResponse(
        task_id=task_id,
        status=status,
        message=message,
    )


@router.get("/status", response_model=SyncStatusResponse)
async def sync_status(
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """获取最近任务、真实步骤进度和定时同步状态。"""
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                id, status, total_rows, processed_rows, error_message,
                started_at, finished_at, trigger_source, phase,
                current_item, total_steps, completed_steps
            FROM _sync_log
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = await cur.fetchone()
        await cur.execute(
            "SELECT MAX(finished_at) FROM _sync_log "
            "WHERE status IN ('success', 'completed')"
        )
        success_row = await cur.fetchone()

    schedule = await get_schedule()
    last_success_at = _iso_utc(success_row[0]) if success_row else None
    if not row:
        return SyncStatusResponse(
            task_id=0,
            status="no_data",
            error_message="暂无同步记录",
            last_success_at=last_success_at,
            schedule=schedule,
        )

    return SyncStatusResponse(
        task_id=row[0],
        status=row[1],
        total_rows=row[2] or 0,
        processed_rows=row[3] or 0,
        error_message=row[4],
        started_at=_iso_utc(row[5]),
        finished_at=_iso_utc(row[6]),
        trigger_source=row[7] or "manual",
        phase=row[8] or "queued",
        current_item=row[9],
        total_steps=row[10] or 0,
        completed_steps=row[11] or 0,
        last_success_at=last_success_at,
        schedule=schedule,
    )


@router.get("/schedule")
async def read_schedule(user: dict = Depends(require_super_admin)):
    """超级管理员读取定时同步配置。"""
    if settings.LOCAL_DATA_SOURCE_ENABLED and not settings.TXDOCS_ENABLED:
        return {"enabled": False, "interval_minutes": 0, "disabled": True,
                "message": "腾讯数据源已下线"}
    return await get_schedule()


@router.put("/schedule")
async def save_schedule(
    payload: SyncScheduleRequest,
    request: Request,
    user: dict = Depends(require_super_admin),
):
    """超级管理员保存定时同步配置。"""
    if settings.LOCAL_DATA_SOURCE_ENABLED and not settings.TXDOCS_ENABLED:
        raise HTTPException(status_code=410, detail="腾讯数据源已下线，不能配置同步")
    try:
        schedule = await update_schedule(
            payload.enabled,
            payload.interval_minutes,
            user["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_admin_audit(
        user,
        "sync.schedule.update",
        target_type="sync_schedule",
        target_name="default",
        detail={
            "enabled": payload.enabled,
            "interval_minutes": payload.interval_minutes,
        },
        **request_audit_fields(request),
    )
    return {"message": "定时同步设置已保存", **schedule}


@router.get("/history")
async def sync_history(
    page: int = 1,
    page_size: int = 20,
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
    conn=Depends(get_db),
):
    """同步历史记录。"""
    del user
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    offset = (page - 1) * page_size
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM _sync_log")
        row = await cur.fetchone()
        total = row[0] if row else 0

        await cur.execute(
            """
            SELECT
                id, status, total_rows, processed_rows, error_message,
                started_at, finished_at, trigger_source, phase,
                current_item, total_steps, completed_steps
            FROM _sync_log
            ORDER BY id DESC
            LIMIT %s OFFSET %s
            """,
            (page_size, offset),
        )
        rows = await cur.fetchall()

    return {
        "data": [
            {
                "task_id": r[0],
                "status": r[1],
                "total_rows": r[2] or 0,
                "processed_rows": r[3] or 0,
                "error_message": r[4],
                "started_at": _iso_utc(r[5]),
                "finished_at": _iso_utc(r[6]),
                "trigger_source": r[7] or "manual",
                "phase": r[8] or "queued",
                "current_item": r[9],
                "total_steps": r[10] or 0,
                "completed_steps": r[11] or 0,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
