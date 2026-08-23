"""管理员和超级管理员可见的统一后台任务队列。"""

from fastapi import APIRouter, Depends

from deps import require_admin_account
from services.admin_task_queue import build_admin_task_queue


router = APIRouter(prefix="/api/admin/task-queue", tags=["管理员后台任务队列"])


@router.get("")
async def admin_task_queue(user: dict = Depends(require_admin_account)):
    del user
    return await build_admin_task_queue()
