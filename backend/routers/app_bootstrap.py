"""客户端启动兼容性接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response

from app_version import APP_VERSION
from config import settings
from database import get_db
from deps import get_bootstrap_user
from services.business_time import (
    DEFAULT_TIMEZONE,
    current_business_date,
    resolve_timezone,
)
from services.client_compatibility import (
    evaluate_client_compatibility,
    minimum_supported_versions,
)
from services.maintenance import load_maintenance_config, maintenance_status


router = APIRouter(prefix="/api/app", tags=["客户端启动"])


def _as_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@router.get("/bootstrap")
async def get_app_bootstrap(
    response: Response,
    client_platform: str | None = Header(
        default=None,
        alias="X-Binhu-Client-Platform",
    ),
    client_version: str | None = Header(
        default=None,
        alias="X-Binhu-Client-Version",
    ),
    user: dict | None = Depends(get_bootstrap_user),
    conn=Depends(get_db),
):
    """返回版本策略、维护状态、业务时间和当前账号能力。"""
    async with conn.cursor() as cur:
        maintenance_config = await load_maintenance_config(cur)
        await cur.execute(
            "SELECT config_value FROM _system_config "
            "WHERE config_key='online_writeback_enabled'"
        )
        writeback_row = await cur.fetchone()
        await cur.execute("SELECT UTC_TIMESTAMP()")
        server_time_row = await cur.fetchone()

    server_time = server_time_row[0] if server_time_row else None
    configured_timezone = maintenance_config.get("timezone") or DEFAULT_TIMEZONE
    timezone_name = getattr(resolve_timezone(configured_timezone), "key", DEFAULT_TIMEZONE)
    maintenance = maintenance_status(maintenance_config, now=server_time)
    maintenance["timezone"] = timezone_name
    compatibility = evaluate_client_compatibility(
        client_platform,
        client_version,
    )
    minimum_versions = minimum_supported_versions()

    response.headers["Cache-Control"] = "no-store"
    return {
        "server_version": APP_VERSION,
        "environment": settings.APP_ENVIRONMENT,
        "environment_label": (
            settings.APP_ENVIRONMENT_LABEL.strip()
            or ("影子压测环境" if settings.APP_ENVIRONMENT == "shadow" else "正式环境")
        ),
        "load_test_run_id": (
            settings.LOAD_TEST_RUN_ID.strip()
            if settings.APP_ENVIRONMENT == "shadow"
            else ""
        ),
        "minimum_supported_versions": minimum_versions,
        "must_upgrade": (
            compatibility["must_upgrade"] or compatibility["write_blocked"]
        ),
        "maintenance": maintenance,
        "business_date": current_business_date(
            timezone_name,
            now=server_time,
        ).isoformat(),
        "timezone": timezone_name,
        "authenticated": user is not None,
        "available_features": sorted(user.get("permissions") or []) if user else [],
        "feature_flags": {
            "registry": settings.REGISTRY_FEATURE_ENABLED,
            "workflow": settings.WORKFLOW_FEATURE_ENABLED,
            "online_writeback": _as_bool(writeback_row[0] if writeback_row else None),
            "client_write_version_enforcement": (
                settings.CLIENT_WRITE_VERSION_ENFORCEMENT_ENABLED
            ),
            "client_write_identification_required": (
                settings.CLIENT_WRITE_IDENTIFICATION_REQUIRED
            ),
        },
        "client": compatibility,
    }
