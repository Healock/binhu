"""Administrator-managed MAC used by the server-side residence lookup."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from database import get_db
from deps import require_permission
from services.audit import record_admin_audit, request_audit_fields
from services.mac_address import (
    SERVER_MAC_CONFIG_KEY,
    current_server_mac,
    normalize_mac_address,
    set_server_mac,
)
from services.permissions import SERVER_MAC_MANAGE
from services.residence_platform_config import clear_residence_sessions
from services.residence_status_scan import wake_residence_lookup_scheduler


router = APIRouter(prefix="/api/system/server-mac", tags=["服务器 MAC 配置"])


class ServerMacUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mac: str = Field(min_length=12, max_length=32)

    @field_validator("mac")
    @classmethod
    def validate_mac(cls, value: str) -> str:
        return normalize_mac_address(value)


@router.get("")
async def get_server_mac(
    _user: dict = Depends(require_permission(SERVER_MAC_MANAGE)),
):
    return {"mac": current_server_mac(), "compatibility_port": 23333}


@router.put("")
async def update_server_mac(
    data: ServerMacUpdate,
    request: Request,
    user: dict = Depends(require_permission(SERVER_MAC_MANAGE)),
    conn=Depends(get_db),
):
    normalized = data.mac
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO _system_config (config_key,config_value) VALUES (%s,%s) "
            "ON DUPLICATE KEY UPDATE config_value=%s",
            (SERVER_MAC_CONFIG_KEY, normalized, normalized),
        )
    await conn.commit()
    set_server_mac(normalized)
    await clear_residence_sessions(conn)
    await record_admin_audit(
        user,
        "system.server_mac.update",
        target_type="system_config",
        target_name=SERVER_MAC_CONFIG_KEY,
        detail={"key": SERVER_MAC_CONFIG_KEY},
        conn=conn,
        **request_audit_fields(request),
    )
    wake_residence_lookup_scheduler(force_full_scan=True)
    return {"mac": normalized, "compatibility_port": 23333}
