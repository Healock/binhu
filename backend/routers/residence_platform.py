"""Configuration and login endpoints for the read-only residence platform."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from database import get_db
from deps import require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.residence_platform_config import (
    RESIDENCE_CONFIG_KEYS,
    clear_residence_sessions,
    load_residence_config,
    public_residence_config,
    serialize_residence_value,
)
from services.residence_status_scan import (
    queue_due_residence_tasks,
    wake_residence_lookup_scheduler,
)


router = APIRouter(prefix="/api/residence-platform", tags=["居住证平台只读查询"])


class ResidenceConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = Field(max_length=500)
    password: str | None = Field(default=None, max_length=500)
    mac_service_url: str = Field(default="http://127.0.0.1:23333", max_length=500)
    timeout_seconds: int = Field(default=15, ge=1, le=120)


async def _save_values(conn, values: dict[str, Any]) -> None:
    async with conn.cursor() as cur:
        for key, value in values.items():
            if key not in RESIDENCE_CONFIG_KEYS:
                continue
            stored = serialize_residence_value(key, value)
            await cur.execute(
                "INSERT INTO _system_config (config_key,config_value) VALUES (%s,%s) "
                "ON DUPLICATE KEY UPDATE config_value=%s",
                (key, stored, stored),
            )


async def _public_config(conn) -> dict[str, Any]:
    config = await load_residence_config(conn)
    payload = public_residence_config(config)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM _communities "
            "WHERE is_active=1 AND qmf_community_code REGEXP '^[0-9A-Z]{10}$'"
        )
        payload["community_account_count"] = int((await cur.fetchone())[0] or 0)
        payload["session_ready"] = bool(
            payload["session_ready"] and payload["community_account_count"]
        )
        await cur.execute(
            "SELECT COUNT(*) FROM _system_config "
            "WHERE LEFT(config_key,%s)=%s",
            (len("residence_session_"), "residence_session_"),
        )
        payload["active_session_count"] = int((await cur.fetchone())[0] or 0)
    return payload


@router.get("/config")
async def get_residence_config(
    _user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    return await _public_config(conn)


@router.put("/config")
async def update_residence_config(
    data: ResidenceConfigUpdate,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    current = await load_residence_config(conn)
    password = data.password if data.password is not None else current.password
    if data.enabled and not all(
        (
            data.base_url.strip(),
            password,
            data.mac_service_url.strip(),
        )
    ):
        raise HTTPException(400, "开启居住证查询前请完整填写接口、统一密码和 MAC 服务")
    values: dict[str, Any] = {
        "residence_lookup_enabled": "1" if data.enabled else "0",
        "residence_base_url": data.base_url.strip().rstrip("/"),
        "residence_mac_service_url": data.mac_service_url.strip().rstrip("/"),
        "residence_timeout_seconds": str(data.timeout_seconds),
    }
    connection_changed = any(
        (
            data.base_url.strip().rstrip("/") != current.base_url,
            data.mac_service_url.strip().rstrip("/") != current.mac_service_url,
        )
    )
    if data.password is not None:
        values["residence_password"] = data.password
    if data.password is not None or connection_changed:
        values["residence_access_token"] = ""
    await _save_values(conn, values)
    if data.password is not None or connection_changed:
        await clear_residence_sessions(conn)
    await record_admin_audit(
        user,
        "residence_platform.config.update",
        target_type="system_config",
        target_name="residence_readonly_lookup",
        detail={"keys": sorted(values)},
        **request_audit_fields(request),
    )
    wake_residence_lookup_scheduler()
    return await _public_config(conn)


@router.post("/scan", status_code=202)
async def start_residence_scan(
    request: Request,
    user: dict = Depends(require_super_admin),
):
    count = await queue_due_residence_tasks(force=True)
    wake_residence_lookup_scheduler()
    await record_admin_audit(
        user,
        "residence_platform.scan.start",
        target_type="external_readonly_scan",
        target_name="mobile_tasks",
        detail={"queued_count": count},
        **request_audit_fields(request),
    )
    return {"queued_count": count}
