"""Runtime configuration for the read-only residence-platform lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.qmf_config import decrypt_secret, encrypt_secret


RESIDENCE_CONFIG_KEYS = {
    "residence_lookup_enabled",
    "residence_base_url",
    "residence_username",
    "residence_password",
    "residence_mac_service_url",
    "residence_access_token",
    "residence_organization_code",
    "residence_timeout_seconds",
}
RESIDENCE_SECRET_KEYS = {
    "residence_username",
    "residence_password",
    "residence_access_token",
}


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, fallback: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class ResidencePlatformConfig:
    enabled: bool
    base_url: str
    username: str
    password: str
    mac_service_url: str
    access_token: str
    organization_code: str
    timeout_seconds: int

    @property
    def credentials_configured(self) -> bool:
        return bool(self.base_url and self.username and self.password and self.mac_service_url)

    @property
    def session_ready(self) -> bool:
        return bool(
            self.enabled
            and self.credentials_configured
            and self.access_token
            and len(self.organization_code.strip()) >= 6
        )


async def load_residence_config(conn) -> ResidencePlatformConfig:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT config_key,config_value FROM _system_config "
            "WHERE config_key LIKE 'residence_%'"
        )
        values = {str(row[0]): row[1] for row in await cur.fetchall()}

    def value(key: str, fallback: str = "") -> str:
        raw = values.get(key)
        if raw is None:
            return fallback
        if key in RESIDENCE_SECRET_KEYS:
            return decrypt_secret(raw)
        return str(raw or "")

    return ResidencePlatformConfig(
        enabled=_as_bool(values.get("residence_lookup_enabled")),
        base_url=value("residence_base_url").rstrip("/"),
        username=value("residence_username"),
        password=value("residence_password"),
        mac_service_url=value(
            "residence_mac_service_url", "http://127.0.0.1:23333"
        ).rstrip("/"),
        access_token=value("residence_access_token"),
        organization_code=value("residence_organization_code"),
        timeout_seconds=_as_int(values.get("residence_timeout_seconds"), 15),
    )


def serialize_residence_value(key: str, value: Any) -> str:
    text = str(value or "")
    return encrypt_secret(text) if key in RESIDENCE_SECRET_KEYS else text


def public_residence_config(config: ResidencePlatformConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "base_url": config.base_url,
        "username": config.username,
        "password_configured": bool(config.password),
        "mac_service_url": config.mac_service_url,
        "access_token_configured": bool(config.access_token),
        "organization_code": config.organization_code,
        "timeout_seconds": config.timeout_seconds,
        "credentials_configured": config.credentials_configured,
        "session_ready": config.session_ready,
    }
