"""Strict read-only client for the residence-platform person lookup."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from services.residence_platform_config import ResidencePlatformConfig


CAPTCHA_PATH_PREFIX = "/sys/randomImage/"
LOGIN_PATH = "/sys/login"
SEARCH_RESIDENT_PATH = "/szjzz/searchIsck"
SEARCH_FLOATING_PATH = "/szjzz/searchzzrk"
READ_ONLY_PATHS = frozenset({SEARCH_RESIDENT_PATH, SEARCH_FLOATING_PATH})


class ResidencePlatformError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResidenceLookupResult:
    state: str
    error_code: str = ""


def _business_code(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_floating_response(payload: Any) -> ResidenceLookupResult:
    """Classify only the two confirmed response contracts."""
    if not isinstance(payload, dict):
        return ResidenceLookupResult("error", "invalid_response")
    code = _business_code(payload.get("code"))
    message = str(payload.get("message") or "").strip()
    result = payload.get("result")
    if payload.get("success") is True and code == 200 and isinstance(result, dict):
        return ResidenceLookupResult("registered")
    if (
        payload.get("success") is False
        and code == 500
        and result is None
        and "没有查询到数据" in message
    ):
        return ResidenceLookupResult("first_registration")
    if code in {401, 403} or any(
        marker in message.lower() for marker in ("token", "登录失效", "未登录", "认证失败")
    ):
        return ResidenceLookupResult("error", "authentication_expired")
    return ResidenceLookupResult("error", "business_error")


def _is_authentication_response(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    code = _business_code(payload.get("code"))
    message = str(payload.get("message") or "").strip().lower()
    return code in {401, 403} or any(
        marker in message for marker in ("token", "登录失效", "未登录", "认证失败")
    )


def _validate_base_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ResidencePlatformError("invalid_config", "居住证平台接口地址无效")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ResidencePlatformError("invalid_config", "居住证平台接口地址格式无效")
    return str(value).rstrip("/")


def _extract_org_code(payload: dict[str, Any]) -> str:
    queue: list[Any] = [payload.get("result")]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            for key in ("orgCode", "org_code", "departCode"):
                value = str(current.get(key) or "").strip()
                if len(value) >= 6 and value[:6].isdigit():
                    return value
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return ""


class ResidencePlatformClient:
    def __init__(
        self,
        config: ResidencePlatformConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = config
        self.base_url = _validate_base_url(config.base_url)
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        # The authorized platform is addressed by IP and currently presents a
        # certificate that cannot be validated by hostname. Restricting every
        # path is therefore part of the transport boundary.
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds),
            verify=False,
            follow_redirects=False,
            transport=self.transport,
        )

    async def fetch_captcha(self) -> tuple[str, str]:
        check_key = str(int(time.time() * 1000))
        async with self._client() as client:
            response = await client.get(
                f"{self.base_url}{CAPTCHA_PATH_PREFIX}{check_key}"
            )
        if response.status_code != 200:
            raise ResidencePlatformError("captcha_http_error", "居住证平台验证码读取失败")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ResidencePlatformError("captcha_invalid_response", "居住证平台验证码响应无法解析") from exc
        image = str(payload.get("result") or "") if isinstance(payload, dict) else ""
        if not payload.get("success") or not image.startswith("data:image/"):
            raise ResidencePlatformError("captcha_business_error", "居住证平台未返回有效验证码")
        return check_key, image

    async def _read_mac(self) -> str:
        url = _validate_base_url(self.config.mac_service_url)
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = await client.get(url)
        if response.status_code != 200:
            raise ResidencePlatformError("mac_service_error", "MAC 服务不可用")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ResidencePlatformError("mac_service_invalid", "MAC 服务响应无法解析") from exc
        mac = str(payload.get("mac") or "").strip() if isinstance(payload, dict) else ""
        if not mac:
            raise ResidencePlatformError("mac_missing", "MAC 服务未返回设备地址")
        return mac

    async def login(
        self,
        *,
        captcha: str = "",
        check_key: str = "",
    ) -> tuple[str, str]:
        if not self.config.credentials_configured:
            raise ResidencePlatformError("config_incomplete", "请先完整填写居住证平台配置")
        effective_check_key = str(check_key or "").strip()
        if not effective_check_key:
            effective_check_key, _ = await self.fetch_captcha()
        body = {
            "username": self.config.username,
            "password": self.config.password,
            "mac": await self._read_mac(),
            "remember_me": True,
            "captcha": str(captcha or "").strip(),
            "checkKey": effective_check_key,
            "terminalType": 1,
        }
        async with self._client() as client:
            response = await client.post(f"{self.base_url}{LOGIN_PATH}", json=body)
        if response.status_code != 200:
            raise ResidencePlatformError("login_http_error", "居住证平台登录失败")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ResidencePlatformError("login_invalid_response", "居住证平台登录响应无法解析") from exc
        if not isinstance(payload, dict) or not payload.get("success"):
            raise ResidencePlatformError(
                "login_rejected",
                "居住证平台拒绝登录，请检查社区账号、统一密码和设备信息",
            )
        result = payload.get("result")
        token = str(result.get("token") or "").strip() if isinstance(result, dict) else ""
        if not token:
            raise ResidencePlatformError("login_token_missing", "居住证平台登录成功但未返回令牌")
        return token, _extract_org_code(payload)

    async def _post_readonly(self, path: str, body: dict[str, str]) -> dict[str, Any]:
        if path not in READ_ONLY_PATHS:
            raise ResidencePlatformError("path_not_allowed", "居住证平台接口不在只读白名单")
        headers = {
            "X-Access-Token": self.config.access_token,
            "tenant_id": "0",
            "Content-Type": "application/json;charset=UTF-8",
        }
        async with self._client() as client:
            response = await client.post(f"{self.base_url}{path}", json=body, headers=headers)
        if response.status_code in {401, 403}:
            raise ResidencePlatformError("authentication_expired", "居住证平台登录已失效")
        if response.status_code != 200:
            raise ResidencePlatformError("http_error", "居住证平台查询返回 HTTP 错误")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ResidencePlatformError("invalid_response", "居住证平台查询响应无法解析") from exc
        if not isinstance(payload, dict):
            raise ResidencePlatformError("invalid_response", "居住证平台查询响应结构异常")
        return payload

    async def lookup(self, identity: str) -> ResidenceLookupResult:
        if not self.config.session_ready:
            raise ResidencePlatformError("session_not_ready", "居住证平台尚未登录")
        body = {"sfzh": identity, "xzqh": self.config.organization_code[:6]}
        resident_payload = await self._post_readonly(SEARCH_RESIDENT_PATH, body)
        if _is_authentication_response(resident_payload):
            raise ResidencePlatformError("authentication_expired", "居住证平台登录已失效")
        result = classify_floating_response(
            await self._post_readonly(SEARCH_FLOATING_PATH, body)
        )
        if result.error_code == "authentication_expired":
            raise ResidencePlatformError(result.error_code, "居住证平台登录已失效")
        return result
