"""Configuration and login endpoints for the read-only residence platform."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from database import get_db
from deps import require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.external_acquisition_jobs import create_job
from services.residence_platform_config import (
    RESIDENCE_CONFIG_KEYS,
    clear_residence_sessions,
    load_residence_config,
    public_residence_config,
    normalize_residence_community_ids,
    serialize_residence_value,
)
from services.residence_status_scan import (
    run_residence_full_scan_job,
    wake_residence_lookup_scheduler,
)


router = APIRouter(prefix="/api/residence-platform", tags=["居住证平台只读查询"])


class ResidenceConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = Field(max_length=500)
    login_community_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(default_factory=list)
    # Kept for older clients during the configuration contract transition.
    login_community_id: int | None = Field(default=None, gt=0)
    password: str | None = Field(default=None, max_length=500)
    mac_service_url: str = Field(default="http://127.0.0.1:23333", max_length=500)
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    full_scan_interval_minutes: int = Field(default=30, ge=5, le=1440)


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
            "SELECT id,name,is_active,qmf_community_code,"
            "CASE WHEN COALESCE(residence_username,'')<>'' THEN 1 ELSE 0 END "
            "FROM _communities ORDER BY is_active DESC,name"
        )
        community_rows = await cur.fetchall()
        payload["community_options"] = [
            {
                "id": int(row[0]),
                "name": str(row[1] or ""),
                "is_active": bool(row[2]),
                "account_configured": bool(row[4]),
            }
            for row in community_rows
        ]
        payload["community_account_count"] = sum(
            1 for row in community_rows if bool(row[2]) and bool(row[4])
        )
        # The offline client must never receive every active community code.
        # Only the selected online lookup scope is safe to cache locally.
        payload["community_codes"] = list(payload.get("login_community_codes") or [])
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
    selected_ids = normalize_residence_community_ids(data.login_community_ids)
    if "login_community_ids" not in data.model_fields_set and data.login_community_id is not None:
        selected_ids = (data.login_community_id,)
    selected_rows: list[tuple[Any, ...]] = []
    async with conn.cursor() as cur:
        for community_id in selected_ids:
            await cur.execute(
                "SELECT id,name,is_active,"
                "CASE WHEN COALESCE(residence_username,'')<>'' THEN 1 ELSE 0 END "
                "FROM _communities WHERE id=%s",
                (community_id,),
            )
            selected_community = await cur.fetchone()
            if not selected_community:
                raise HTTPException(400, "所选登录社区不存在")
            selected_rows.append(selected_community)
    for selected_community in selected_rows:
        if not bool(selected_community[2]):
            raise HTTPException(400, "所选登录社区已停用，请在社区管理中重新启用或改选其他社区")
        if not bool(selected_community[3]):
            raise HTTPException(400, "所选社区尚未配置居住证完整登录账号，请先到社区管理填写")
    if data.enabled and not all(
        (
            data.base_url.strip(),
            selected_ids,
            selected_rows and len(selected_rows) == len(selected_ids),
            password,
            data.mac_service_url.strip(),
        )
    ):
        raise HTTPException(
            400,
            "开启居住证查询前请选择已配置账号的社区，并填写接口、统一密码和 MAC 服务",
        )
    values: dict[str, Any] = {
        "residence_lookup_enabled": "1" if data.enabled else "0",
        "residence_base_url": data.base_url.strip().rstrip("/"),
        "residence_login_community_ids": json.dumps(selected_ids, separators=(",", ":")),
        "residence_login_community_id": str(selected_ids[0] if selected_ids else ""),
        "residence_mac_service_url": data.mac_service_url.strip().rstrip("/"),
        "residence_timeout_seconds": str(data.timeout_seconds),
        "residence_full_scan_interval_minutes": str(data.full_scan_interval_minutes),
    }
    scope_changed = selected_ids != current.login_community_ids
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
    elif scope_changed:
        previous_ids = set(current.login_community_ids)
        selected_id_set = set(selected_ids)
        for affected_id in sorted(previous_ids.symmetric_difference(selected_id_set)):
            await clear_residence_sessions(conn, f"community_{affected_id}")
    await record_admin_audit(
        user,
        "residence_platform.config.update",
        target_type="system_config",
        target_name="residence_readonly_lookup",
        detail={"keys": sorted(values)},
        **request_audit_fields(request),
    )
    wake_residence_lookup_scheduler(force_full_scan=True)
    return await _public_config(conn)


@router.post("/scan", status_code=202)
async def start_residence_scan(
    request: Request,
    user: dict = Depends(require_super_admin),
):
    run, reused = await create_job(
        "residence_full_scan",
        int(user["id"]),
        {},
        run_residence_full_scan_job,
        dedupe_key="residence_full_scan",
    )
    await record_admin_audit(
        user,
        "residence_platform.scan.start",
        target_type="external_readonly_scan",
        target_name="mobile_tasks",
        detail={"run_id": run.get("id"), "reused": reused},
        **request_audit_fields(request),
    )
    return {"run": run, "reused": reused}
