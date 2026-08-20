from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import get_current_user
from services.external_acquisition_jobs import get_job, latest_job
from services.permissions import VISIT_SOURCE_MANAGE, WORKFLOW_CONFIG_MANAGE

router = APIRouter(prefix="/api/external-acquisition", tags=["外部数据后台任务"])


@router.get("/runs/{run_id}")
async def external_run(run_id: int, user: dict = Depends(get_current_user)):
    run = await get_job(run_id)
    if not run:
        raise HTTPException(404, "外部获取任务不存在")
    needed = WORKFLOW_CONFIG_MANAGE if run["kind"].startswith("photo_sheet_") else VISIT_SOURCE_MANAGE
    if needed not in set(user.get("permissions") or []):
        raise HTTPException(403, "无权查看该外部获取任务")
    return run


@router.get("/latest")
async def external_latest(
    kind: str = Query(..., min_length=1, max_length=50),
    user: dict = Depends(get_current_user),
):
    needed = WORKFLOW_CONFIG_MANAGE if kind.startswith("photo_sheet_") else VISIT_SOURCE_MANAGE
    if needed not in set(user.get("permissions") or []):
        raise HTTPException(403, "无权查看该外部获取任务")
    return {"data": await latest_job(kind)}
