"""已下线的腾讯同步接口。

路由保留用于旧客户端得到明确状态；所有接口都不会读取配置、创建任务或
访问腾讯文档。
"""

from fastapi import APIRouter, Depends, HTTPException

from deps import get_current_user, require_permission, require_super_admin
from schemas.sync import SyncStatusResponse, SyncTriggerResponse
from services.permissions import ONLINE_SUMMARY_VIEW, SYNC_TRIGGER


router = APIRouter(prefix="/api/sync", tags=["已下线的数据同步"])


@router.post("/trigger", response_model=SyncTriggerResponse)
async def trigger_sync(user: dict = Depends(require_permission(SYNC_TRIGGER))):
    del user
    return SyncTriggerResponse(
        task_id=0,
        status="disabled",
        message="腾讯数据源已下线，当前业务数据由本地任务池提供",
    )


@router.get("/status", response_model=SyncStatusResponse)
async def sync_status(user: dict = Depends(get_current_user)):
    del user
    return SyncStatusResponse(
        task_id=0,
        status="disabled",
        error_message="腾讯数据源已下线",
        phase="disabled",
        schedule={"enabled": False, "interval_minutes": 0},
    )


@router.get("/schedule")
async def read_schedule(user: dict = Depends(require_super_admin)):
    del user
    return {
        "enabled": False,
        "interval_minutes": 0,
        "next_run_at": None,
        "server_time": None,
        "disabled": True,
        "message": "腾讯数据源已下线",
    }


@router.put("/schedule")
async def save_schedule(user: dict = Depends(require_super_admin)):
    del user
    raise HTTPException(status_code=410, detail="腾讯数据源已下线，不能配置同步")


@router.get("/history")
async def sync_history(user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW))):
    del user
    return {
        "data": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
        "message": "腾讯数据源已下线；历史记录仅保留在受限审计表中",
    }
