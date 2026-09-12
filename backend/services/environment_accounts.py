"""Production control-plane client for isolated environment accounts."""
from __future__ import annotations

import json
from typing import Any

import httpx

from config import settings

_ALLOWED = {"development", "staging"}


class EnvironmentAccountGatewayError(RuntimeError):
    def __init__(self, environment: str, reason: str = "unavailable"):
        super().__init__(reason)
        self.environment = environment
        self.reason = reason


def gateway_urls() -> dict[str, str]:
    raw = settings.ENVIRONMENT_ACCOUNT_GATEWAY_URLS.strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EnvironmentAccountGatewayError("unknown", "configuration_invalid") from exc
    if not isinstance(value, dict):
        raise EnvironmentAccountGatewayError("unknown", "configuration_invalid")
    result = {}
    for environment, url in value.items():
        if environment in _ALLOWED and isinstance(url, str) and url.startswith(("http://", "https://")):
            result[environment] = url.rstrip("/")
    return result


def _url(environment: str) -> str:
    if environment not in _ALLOWED:
        raise EnvironmentAccountGatewayError(environment, "environment_not_allowed")
    value = gateway_urls().get(environment)
    if not value:
        raise EnvironmentAccountGatewayError(environment, "gateway_not_configured")
    return value


def _headers(environment: str) -> dict[str, str]:
    raw = settings.ENVIRONMENT_ACCOUNT_GATEWAY_TOKENS.strip()
    try:
        values = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise EnvironmentAccountGatewayError(environment, "configuration_invalid") from exc
    token = values.get(environment) if isinstance(values, dict) else None
    if not isinstance(token, str) or not token:
        raise EnvironmentAccountGatewayError(environment, "gateway_token_not_configured")
    return {"X-Environment-Account-Token": token}


async def list_accounts(environment: str) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f"{_url(environment)}/api/internal/environment-accounts", headers=_headers(environment))
            response.raise_for_status()
            payload = response.json()
    except EnvironmentAccountGatewayError:
        raise
    except Exception as exc:
        raise EnvironmentAccountGatewayError(environment) from exc
    if payload.get("environment") != environment or not isinstance(payload.get("accounts"), list):
        raise EnvironmentAccountGatewayError(environment, "identity_mismatch")
    return [
        {
            "environment": environment,
            "username": str(item.get("username", "")),
            "display_name": str(item.get("display_name", "")),
            "status": str(item.get("status", "unknown")),
            "password_is_temporary": bool(item.get("password_is_temporary")),
            "last_login_at": item.get("last_login_at"),
            "last_reset_at": item.get("last_reset_at"),
        }
        for item in payload["accounts"]
    ]


async def reset_account(environment: str, username: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{_url(environment)}/api/internal/environment-accounts/reset",
                headers=_headers(environment),
                json={"username": username},
            )
            response.raise_for_status()
            payload = response.json()
    except EnvironmentAccountGatewayError:
        raise
    except Exception as exc:
        raise EnvironmentAccountGatewayError(environment) from exc
    if payload.get("environment") != environment or payload.get("username") != username:
        raise EnvironmentAccountGatewayError(environment, "identity_mismatch")
    password = payload.get("temporary_password")
    if not isinstance(password, str) or not password:
        raise EnvironmentAccountGatewayError(environment, "reset_response_invalid")
    return {
        "environment": environment,
        "username": username,
        "temporary_password": password,
        "password_is_temporary": True,
        "issued_at": payload.get("issued_at"),
    }
