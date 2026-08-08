"""系统配置 API"""

from fastapi import APIRouter, Depends, HTTPException, Request
from database import get_db
from deps import require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.personnel_positions import (
    POSITION_CONFIG_KEYS,
    VISIT_POSITION_CONFIG_KEY,
    serialize_position_config,
    serialize_rental_position_config,
)
from services.maintenance import validate_maintenance_config

router = APIRouter(
    prefix="/api/system",
    tags=["系统配置"],
    dependencies=[Depends(require_super_admin)],
)


@router.get("/config")
async def get_config(conn=Depends(get_db)):
    """获取系统配置"""
    async with conn.cursor() as cur:
        await cur.execute("SELECT config_key, config_value FROM _system_config")
        rows = await cur.fetchall()
    return {"data": {r[0]: r[1] for r in rows}}


@router.put("/config")
async def update_config(
    config: dict,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    """更新系统配置"""
    async with conn.cursor() as cur:
        maintenance_updates: dict[str, str] = {}
        maintenance_keys = {
            "maintenance_enabled",
            "maintenance_start_at",
            "maintenance_end_at",
            "maintenance_message",
        }
        if maintenance_keys.intersection(config):
            await cur.execute(
                "SELECT config_key, config_value FROM _system_config "
                "WHERE config_key IN "
                "('maintenance_enabled', 'maintenance_start_at', "
                "'maintenance_end_at', 'maintenance_message')"
            )
            merged = {
                str(row[0]): str(row[1] or "")
                for row in await cur.fetchall()
            }
            merged.update({key: config[key] for key in maintenance_keys if key in config})
            try:
                maintenance_updates = validate_maintenance_config(merged)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        for k, v in config.items():
            if k in maintenance_updates:
                continue
            if k == "session_idle_minutes":
                try:
                    v = int(v)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(400, "空闲超时时间必须是分钟数") from exc
                if v < 5 or v > 1440:
                    raise HTTPException(400, "空闲超时时间必须在 5 分钟至 24 小时之间")
            if k == "permission_enforcement_enabled":
                raise HTTPException(400, "权限启用状态只能通过迁移工具修改")
            if k == "online_writeback_enabled":
                normalized = str(v).strip().lower()
                if normalized not in {"0", "1", "true", "false"}:
                    raise HTTPException(400, "在线回写开关必须是开启或关闭")
                v = "1" if normalized in {"1", "true"} else "0"
            if k in POSITION_CONFIG_KEYS:
                try:
                    v = (
                        serialize_rental_position_config(v)
                        if k == VISIT_POSITION_CONFIG_KEY
                        else serialize_position_config(v)
                    )
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            await cur.execute(
                "INSERT INTO _system_config (config_key, config_value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE config_value = %s",
                (k, str(v), str(v)),
            )
        for k, v in maintenance_updates.items():
            await cur.execute(
                "INSERT INTO _system_config (config_key, config_value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE config_value = %s",
                (k, v, v),
            )
    await record_admin_audit(
        user,
        "system.config.update",
        target_type="system_config",
        target_name=",".join(sorted(str(key) for key in config)),
        detail={"keys": sorted(str(key) for key in config)},
        **request_audit_fields(request),
    )
    return {"message": "配置已更新"}
