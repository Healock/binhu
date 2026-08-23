"""管理员和超级管理员可见的统一后台任务队列。"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from database import db_manager
from deps import require_admin_account
from routers.workflow_photo_sheet import retry_photo_sheet_outbox
from services.admin_task_queue import (
    build_admin_task_queue,
    get_admin_task_queue_details,
)


router = APIRouter(prefix="/api/admin/task-queue", tags=["管理员后台任务队列"])


@router.get("")
async def admin_task_queue(user: dict = Depends(require_admin_account)):
    del user
    return await build_admin_task_queue()


@router.get("/{source}/details")
async def admin_task_queue_details(
    source: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    user: dict = Depends(require_admin_account),
):
    del user
    try:
        return await get_admin_task_queue_details(
            source,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="该任务队列不支持查看明细") from exc


@router.post("/photo-writeback/{outbox_id}/retry")
async def retry_admin_photo_writeback(
    outbox_id: int,
    request: Request,
    user: dict = Depends(require_admin_account),
):
    pool = db_manager.get_pool("workflow")
    async with pool.acquire() as conn:
        return await retry_photo_sheet_outbox(
            outbox_id,
            request,
            user=user,
            conn=conn,
        )
