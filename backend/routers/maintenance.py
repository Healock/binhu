"""公开维护状态接口。"""

from fastapi import APIRouter, Depends, Response

from database import get_db
from services.maintenance import load_maintenance_config, maintenance_status


router = APIRouter(prefix="/api/maintenance", tags=["维护状态"])


@router.get("/status")
async def get_maintenance_status(response: Response, conn=Depends(get_db)):
    """登录页使用的非敏感维护状态；不返回账号、任务或数据库信息。"""
    async with conn.cursor() as cur:
        config = await load_maintenance_config(cur)
        await cur.execute("SELECT UTC_TIMESTAMP()")
        server_time_row = await cur.fetchone()
    response.headers["Cache-Control"] = "no-store"
    return maintenance_status(
        config,
        now=server_time_row[0] if server_time_row else None,
    )
