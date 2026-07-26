"""系统配置 API"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from database import get_db

router = APIRouter(prefix="/api/system", tags=["系统配置"])


@router.get("/config")
async def get_config(conn=Depends(get_db)):
    """获取系统配置"""
    async with conn.cursor() as cur:
        await cur.execute("SELECT config_key, config_value FROM _system_config")
        rows = await cur.fetchall()
    return {"data": {r[0]: r[1] for r in rows}}


@router.put("/config")
async def update_config(config: dict, conn=Depends(get_db)):
    """更新系统配置"""
    async with conn.cursor() as cur:
        for k, v in config.items():
            await cur.execute(
                "INSERT INTO _system_config (config_key, config_value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE config_value = %s",
                (k, str(v), str(v)),
            )
    return {"message": "配置已更新"}
