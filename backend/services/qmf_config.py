"""Runtime configuration for the全民防模型三 preview and sealed registration.

The preview originally read all values from process environment variables.  The
settings page now persists the values in ``_system_config`` so an administrator
does not need shell access.  Credentials and device identifiers are encrypted
at rest with a key derived from the existing application encryption key.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from config import settings


QMF_CONFIG_KEYS = {
    "qmf_preview_enabled",
    "qmf_registration_enabled",
    "qmf_login_protocol_verified",
    "qmf_write_protocol_verified",
    "qmf_api_base_url",
    "qmf_login_host",
    "qmf_login_port",
    "qmf_source_username",
    "qmf_source_password",
    "qmf_source_imei",
    "qmf_source_machine_uid",
    "qmf_expected_station_code",
    "qmf_expected_station_name",
    "qmf_timeout_seconds",
    "qmf_session_max_seconds",
}
QMF_SECRET_KEYS = {
    "qmf_source_username",
    "qmf_source_password",
    "qmf_source_imei",
    "qmf_source_machine_uid",
}


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, fallback: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return fallback


def _fernet() -> Fernet:
    digest = hashlib.sha256(str(settings.ENCRYPTION_KEY).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return "v1:" + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if not text.startswith("v1:"):
        # Never treat an unexpected database value as a usable credential.
        return ""
    try:
        return _fernet().decrypt(text[3:].encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeError, InvalidToken):
        return ""


@dataclass(frozen=True)
class QmfRuntimeConfig:
    preview_enabled: bool
    registration_enabled: bool
    login_protocol_verified: bool
    write_protocol_verified: bool
    preview_allowed_username: str
    api_base_url: str
    login_host: str
    login_port: int
    source_username: str
    source_password: str
    source_imei: str
    source_machine_uid: str
    expected_station_code: str
    expected_station_name: str
    timeout_seconds: int
    session_max_seconds: int

    @property
    def configured(self) -> bool:
        return bool(
            self.preview_enabled
            and self.login_protocol_verified
            and self.preview_allowed_username == "shenshenghua"
            and all(
                (
                    self.api_base_url,
                    self.login_host,
                    self.login_port,
                    self.source_username,
                    self.source_password,
                    self.source_imei,
                    self.source_machine_uid,
                    self.expected_station_code,
                    self.expected_station_name,
                )
            )
        )

    @property
    def registration_configured(self) -> bool:
        return bool(
            self.configured
            and self.registration_enabled
            and self.write_protocol_verified
        )


def settings_config() -> QmfRuntimeConfig:
    """Build the fallback configuration from environment settings."""
    return QmfRuntimeConfig(
        preview_enabled=bool(settings.QMF_PREVIEW_ENABLED),
        registration_enabled=bool(settings.QMF_REGISTRATION_ENABLED),
        login_protocol_verified=bool(settings.QMF_LOGIN_PROTOCOL_VERIFIED),
        write_protocol_verified=bool(settings.QMF_WRITE_PROTOCOL_VERIFIED),
        preview_allowed_username=str(settings.QMF_PREVIEW_ALLOWED_USERNAME or ""),
        api_base_url=str(settings.QMF_API_BASE_URL or ""),
        login_host=str(settings.QMF_LOGIN_HOST or ""),
        login_port=int(settings.QMF_LOGIN_PORT or 0),
        source_username=str(settings.QMF_SOURCE_USERNAME or ""),
        source_password=str(settings.QMF_SOURCE_PASSWORD or ""),
        source_imei=str(settings.QMF_SOURCE_IMEI or ""),
        source_machine_uid=str(settings.QMF_SOURCE_MACHINE_UID or ""),
        expected_station_code=str(settings.QMF_EXPECTED_STATION_CODE or ""),
        expected_station_name=str(settings.QMF_EXPECTED_STATION_NAME or ""),
        timeout_seconds=max(1, int(settings.QMF_TIMEOUT_SECONDS or 15)),
        session_max_seconds=max(1, int(settings.QMF_SESSION_MAX_SECONDS or 45)),
    )


def _config_value(row: dict[str, Any], key: str, fallback: str) -> str:
    value = row.get(key)
    if value is None:
        return fallback
    if key in QMF_SECRET_KEYS:
        return decrypt_secret(value)
    return str(value or "")


async def load_qmf_config(conn) -> QmfRuntimeConfig:
    """Load database overrides, falling back to process configuration."""
    fallback = settings_config()
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT config_key, config_value FROM _system_config "
            "WHERE config_key LIKE 'qmf_%'"
        )
        rows = await cur.fetchall()
    values = {str(row[0]): row[1] for row in rows}
    return QmfRuntimeConfig(
        preview_enabled=_as_bool(values.get("qmf_preview_enabled", fallback.preview_enabled)),
        registration_enabled=_as_bool(
            values.get("qmf_registration_enabled", fallback.registration_enabled)
        ),
        login_protocol_verified=_as_bool(
            values.get("qmf_login_protocol_verified", fallback.login_protocol_verified)
        ),
        write_protocol_verified=_as_bool(
            values.get("qmf_write_protocol_verified", fallback.write_protocol_verified)
        ),
        preview_allowed_username=_config_value(
            values, "qmf_preview_allowed_username", fallback.preview_allowed_username
        ),
        api_base_url=_config_value(values, "qmf_api_base_url", fallback.api_base_url),
        login_host=_config_value(values, "qmf_login_host", fallback.login_host),
        login_port=_as_int(values.get("qmf_login_port", fallback.login_port), fallback.login_port),
        source_username=_config_value(values, "qmf_source_username", fallback.source_username),
        source_password=_config_value(values, "qmf_source_password", fallback.source_password),
        source_imei=_config_value(values, "qmf_source_imei", fallback.source_imei),
        source_machine_uid=_config_value(
            values, "qmf_source_machine_uid", fallback.source_machine_uid
        ),
        expected_station_code=_config_value(
            values, "qmf_expected_station_code", fallback.expected_station_code
        ),
        expected_station_name=_config_value(
            values, "qmf_expected_station_name", fallback.expected_station_name
        ),
        timeout_seconds=_as_int(
            values.get("qmf_timeout_seconds", fallback.timeout_seconds),
            fallback.timeout_seconds,
            1,
        ),
        session_max_seconds=_as_int(
            values.get("qmf_session_max_seconds", fallback.session_max_seconds),
            fallback.session_max_seconds,
            1,
        ),
    )


def public_config(config: QmfRuntimeConfig, stored_keys: set[str]) -> dict[str, Any]:
    """Return the settings-page representation.

    Device identifiers are intentionally returned in full for the restricted
    system-settings page.  The password is write-only and never returned.
    """
    return {
        "preview_enabled": config.preview_enabled,
        "registration_enabled": config.registration_enabled,
        "login_protocol_verified": config.login_protocol_verified,
        "write_protocol_verified": config.write_protocol_verified,
        "preview_allowed_username": config.preview_allowed_username,
        "api_base_url": config.api_base_url,
        "login_host": config.login_host,
        "login_port": config.login_port,
        "source_username": config.source_username,
        "source_password_configured": bool(config.source_password),
        "source_imei": config.source_imei,
        "source_machine_uid": config.source_machine_uid,
        "expected_station_code": config.expected_station_code,
        "expected_station_name": config.expected_station_name,
        "timeout_seconds": config.timeout_seconds,
        "session_max_seconds": config.session_max_seconds,
        "configured": config.configured,
        "registration_configured": config.registration_configured,
        "database_keys": sorted(stored_keys),
    }


def serialize_value(key: str, value: Any) -> str:
    text = str(value or "")
    return encrypt_secret(text) if key in QMF_SECRET_KEYS else text
