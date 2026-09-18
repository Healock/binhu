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
    "residence_login_community_id",
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
SESSION_SCOPE_PATTERN = re.compile(r"(?:[0-9A-Z]{10}|community_[1-9][0-9]*)")


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
    login_community_id: int | None = None
    login_community_name: str = ""
    login_community_code: str = ""

    @property
    def credentials_configured(self) -> bool:
        return bool(
            self.login_community_id
            and self.base_url
            and self.username
            and self.password
            and self.mac_service_url
        )

    @property
    def session_ready(self) -> bool:
        # Community accounts are logged in lazily by the background worker.
        return bool(self.enabled and self.credentials_configured)


@dataclass(frozen=True)
class ResidenceCommunitySession:
    token: str
    organization_code: str


def _session_key(session_scope: str) -> str:
    scope = str(session_scope or "").strip().upper()
    if not SESSION_SCOPE_PATTERN.fullmatch(scope):
        raise ValueError("invalid_residence_session_scope")
    return f"{RESIDENCE_SESSION_PREFIX}{scope}"


async def load_residence_session(conn, session_scope: str) -> ResidenceCommunitySession | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT config_value FROM _system_config WHERE config_key=%s",
            (_session_key(session_scope),),
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
    session_scope: str,
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
            (_session_key(session_scope), stored, stored),
        )
    await conn.commit()


async def clear_residence_sessions(conn, session_scope: str = "") -> None:
    async with conn.cursor() as cur:
        if session_scope:
            await cur.execute(
                "DELETE FROM _system_config WHERE config_key=%s",
                (_session_key(session_scope),),
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

    login_community_id = None
    try:
        candidate_id = int(values.get("residence_login_community_id") or 0)
        login_community_id = candidate_id if candidate_id > 0 else None
    except (TypeError, ValueError):
        login_community_id = None
    selected_username = ""
    selected_name = ""
    selected_code = ""
    if login_community_id is not None:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name,qmf_community_code,residence_username,is_active "
                "FROM _communities WHERE id=%s",
                (login_community_id,),
            )
            selected = await cur.fetchone()
        if selected:
            selected_name = str(selected[0] or "").strip()
            selected_code = str(selected[1] or "").strip().upper()
            if bool(selected[3]) and selected[2]:
                selected_username = decrypt_secret(selected[2]).strip()

    return ResidencePlatformConfig(
        enabled=_as_bool(values.get("residence_lookup_enabled")),
        base_url=value("residence_base_url").rstrip("/"),
        username=selected_username,
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
        login_community_id=login_community_id,
        login_community_name=selected_name,
        login_community_code=selected_code,
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
        "username": config.username,
        "login_community_id": config.login_community_id,
        "login_community_name": config.login_community_name,
        "selected_account_configured": bool(config.username),
        "account_mode": "selected_community_account",
        "login_mode": "automatic_hidden_challenge",
    }
