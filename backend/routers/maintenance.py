"""公开维护状态接口。"""

from fastapi import APIRouter, Depends, Response

from database import get_db
from services.maintenance import load_maintenance_config, maintenance_config_cache, maintenance_status


router = APIRouter(prefix="/api/maintenance", tags=["维护状态"])


@router.get("/status")
async def get_maintenance_status(response: Response, conn=Depends(get_db)):
    """登录页使用的非敏感维护状态；不返回账号、任务或数据库信息。"""
    config = maintenance_config_cache.get("config")
    if config is None:
        async with conn.cursor() as cur:
            config = await load_maintenance_config(cur)
        maintenance_config_cache.set("config", config)
    response.headers["Cache-Control"] = "private, max-age=2"
    return maintenance_status(config)
