"""Configuration and login endpoints for the read-only residence platform."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from database import get_db
from deps import require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.residence_platform import ResidencePlatformClient, ResidencePlatformError
from services.residence_platform_config import (
    RESIDENCE_CONFIG_KEYS,
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
    username: str = Field(default="", max_length=200)
    password: str | None = Field(default=None, max_length=500)
    mac_service_url: str = Field(default="http://127.0.0.1:23333", max_length=500)
    organization_code: str = Field(default="", max_length=100)
    timeout_seconds: int = Field(default=15, ge=1, le=120)


class ResidenceLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captcha: str = Field(min_length=1, max_length=20)
    check_key: str = Field(min_length=1, max_length=40)


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


@router.get("/config")
async def get_residence_config(
    _user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    return public_residence_config(await load_residence_config(conn))


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
            data.username.strip(),
            password,
            data.mac_service_url.strip(),
        )
    ):
        raise HTTPException(400, "开启居住证查询前请完整填写接口、账号、密码和 MAC 服务")
    values: dict[str, Any] = {
        "residence_lookup_enabled": "1" if data.enabled else "0",
        "residence_base_url": data.base_url.strip().rstrip("/"),
        "residence_username": data.username.strip(),
        "residence_mac_service_url": data.mac_service_url.strip().rstrip("/"),
        "residence_organization_code": data.organization_code.strip(),
        "residence_timeout_seconds": str(data.timeout_seconds),
    }
    connection_changed = any(
        (
            data.base_url.strip().rstrip("/") != current.base_url,
            data.username.strip() != current.username,
            data.mac_service_url.strip().rstrip("/") != current.mac_service_url,
            data.organization_code.strip() != current.organization_code,
        )
    )
    if data.password is not None:
        values["residence_password"] = data.password
    if data.password is not None or connection_changed:
        values["residence_access_token"] = ""
    await _save_values(conn, values)
    await record_admin_audit(
        user,
        "residence_platform.config.update",
        target_type="system_config",
        target_name="residence_readonly_lookup",
        detail={"keys": sorted(values)},
        **request_audit_fields(request),
    )
    wake_residence_lookup_scheduler()
    return public_residence_config(await load_residence_config(conn))


@router.post("/captcha")
async def fetch_residence_captcha(
    _user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    try:
        check_key, image = await ResidencePlatformClient(
            await load_residence_config(conn)
        ).fetch_captcha()
        return {"check_key": check_key, "image": image}
    except ResidencePlatformError as exc:
        raise HTTPException(502, {"code": exc.code, "message": str(exc)}) from exc


@router.post("/login")
async def login_residence_platform(
    data: ResidenceLoginRequest,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    config = await load_residence_config(conn)
    try:
        token, detected_org = await ResidencePlatformClient(config).login(
            captcha=data.captcha,
            check_key=data.check_key,
        )
    except ResidencePlatformError as exc:
        raise HTTPException(502, {"code": exc.code, "message": str(exc)}) from exc
    organization_code = detected_org or config.organization_code
    if len(organization_code.strip()) < 6:
        raise HTTPException(400, "登录成功，但未能确定账号所属机构代码；请先在设置中填写")
    await _save_values(
        conn,
        {
            "residence_access_token": token,
            "residence_organization_code": organization_code,
        },
    )
    await record_admin_audit(
        user,
        "residence_platform.login",
        target_type="external_session",
        target_name="residence_platform",
        detail={"organization_prefix": organization_code[:6]},
        **request_audit_fields(request),
    )
    await queue_due_residence_tasks(force=True)
    wake_residence_lookup_scheduler()
    return public_residence_config(await load_residence_config(conn))


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
