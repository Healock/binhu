"""Runtime configuration for the read-only residence-platform lookup."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
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
    "residence_full_scan_interval_minutes",
}
RESIDENCE_SECRET_KEYS = {
    "residence_username",
    "residence_password",
    "residence_access_token",
}
RESIDENCE_SESSION_PREFIX = "residence_session_"
COMMUNITY_CODE_PATTERN = re.compile(r"[0-9A-Z]{10}")


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
    full_scan_interval_minutes: int

    @property
    def credentials_configured(self) -> bool:
        return bool(self.base_url and self.password and self.mac_service_url)

    @property
    def session_ready(self) -> bool:
        # Community accounts are logged in lazily by the background worker.
        return bool(self.enabled and self.credentials_configured)


@dataclass(frozen=True)
class ResidenceCommunitySession:
    token: str
    organization_code: str


def residence_username(community_code: str) -> str:
    code = str(community_code or "").strip().upper()
    if not COMMUNITY_CODE_PATTERN.fullmatch(code):
        raise ValueError("invalid_community_code")
    return f"{code}00"


def _session_key(community_code: str) -> str:
    residence_username(community_code)
    return f"{RESIDENCE_SESSION_PREFIX}{community_code.strip().upper()}"


async def load_residence_session(conn, community_code: str) -> ResidenceCommunitySession | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT config_value FROM _system_config WHERE config_key=%s",
            (_session_key(community_code),),
        )
        row = await cur.fetchone()
    if not row:
        return None
    try:
        payload = json.loads(decrypt_secret(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    token = str(payload.get("token") or "").strip()
    organization_code = str(payload.get("organization_code") or "").strip()
    if not token or len(organization_code) < 6:
        return None
    return ResidenceCommunitySession(token=token, organization_code=organization_code)


async def save_residence_session(
    conn,
    community_code: str,
    session: ResidenceCommunitySession,
) -> None:
    stored = encrypt_secret(json.dumps({
        "token": session.token,
        "organization_code": session.organization_code,
    }, ensure_ascii=True, separators=(",", ":")))
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO _system_config (config_key,config_value) VALUES (%s,%s) "
            "ON DUPLICATE KEY UPDATE config_value=%s",
            (_session_key(community_code), stored, stored),
        )
    await conn.commit()


async def clear_residence_sessions(conn, community_code: str = "") -> None:
    async with conn.cursor() as cur:
        if community_code:
            await cur.execute(
                "DELETE FROM _system_config WHERE config_key=%s",
                (_session_key(community_code),),
            )
        else:
            await cur.execute(
                "DELETE FROM _system_config WHERE LEFT(config_key,%s)=%s",
                (len(RESIDENCE_SESSION_PREFIX), RESIDENCE_SESSION_PREFIX),
            )
    await conn.commit()


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
        full_scan_interval_minutes=min(
            1440,
            max(5, _as_int(values.get("residence_full_scan_interval_minutes"), 30)),
        ),
    )


def serialize_residence_value(key: str, value: Any) -> str:
    text = str(value or "")
    return encrypt_secret(text) if key in RESIDENCE_SECRET_KEYS else text


def public_residence_config(config: ResidencePlatformConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "base_url": config.base_url,
        "password_configured": bool(config.password),
        "mac_service_url": config.mac_service_url,
        "timeout_seconds": config.timeout_seconds,
        "full_scan_interval_minutes": config.full_scan_interval_minutes,
        "credentials_configured": config.credentials_configured,
        "session_ready": config.session_ready,
        "account_mode": "community_code_suffix_00",
        "login_mode": "automatic_hidden_challenge",
    }
