"""Strict read-only client for the residence-platform person lookup."""

from __future__ import annotations

import base64
import binascii
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from services.residence_platform_config import ResidencePlatformConfig


CAPTCHA_PATH_PREFIX = "/sys/randomImage/"
LOGIN_PATH = "/sys/login"
SEARCH_RESIDENT_PATH = "/szjzz/searchIsck"
SEARCH_FLOATING_PATH = "/szjzz/searchzzrk"
SEARCH_PHOTO_PATH = "/szjzz/searchPhoto"
READ_ONLY_PATHS = frozenset({
    SEARCH_RESIDENT_PATH,
    SEARCH_FLOATING_PATH,
    SEARCH_PHOTO_PATH,
})
MAX_PHOTO_BYTES = 5 * 1024 * 1024

NATION_LABELS = {
    "01": "汉族", "02": "蒙古族", "03": "回族", "04": "藏族",
    "05": "维吾尔族", "06": "苗族", "07": "彝族", "08": "壮族",
    "09": "布依族", "10": "朝鲜族", "11": "满族", "12": "侗族",
    "13": "瑶族", "14": "白族", "15": "土家族", "16": "哈尼族",
    "17": "哈萨克族", "18": "傣族", "19": "黎族", "20": "傈僳族",
    "21": "佤族", "22": "畲族", "23": "高山族", "24": "拉祜族",
    "25": "水族", "26": "东乡族", "27": "纳西族", "28": "景颇族",
    "29": "柯尔克孜族", "30": "土族", "31": "达斡尔族", "32": "仫佬族",
    "33": "羌族", "34": "布朗族", "35": "撒拉族", "36": "毛南族",
    "37": "仡佬族", "38": "锡伯族", "39": "阿昌族", "40": "普米族",
    "41": "塔吉克族", "42": "怒族", "43": "乌孜别克族", "44": "俄罗斯族",
    "45": "鄂温克族", "46": "德昂族", "47": "保安族", "48": "裕固族",
    "49": "京族", "50": "塔塔尔族", "51": "独龙族", "52": "鄂伦春族",
    "53": "赫哲族", "54": "门巴族", "55": "珞巴族", "56": "基诺族",
    "97": "其他", "98": "外国血统中国籍人士", "99": "未说明民族",
}


class ResidencePlatformError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResidenceLookupResult:
    state: str
    error_code: str = ""


@dataclass(frozen=True)
class ResidenceRegistrationDetail:
    birth_date: str
    age: int | None
    ethnicity: str
    household_area_code: str
    household_detail: str
    registered_address: str
    registration_status: str
    registration_status_text: str
    updated_at: str
    photo_data_url: str = ""
    photo_state: str = "missing"
    photo_error_code: str = ""


def normalize_birth_date(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())
    if len(text) != 8:
        return ""
    try:
        parsed = datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return ""
    return parsed.isoformat()


def calculate_age(birth_date: str, *, today: date | None = None) -> int | None:
    try:
        born = date.fromisoformat(str(birth_date or ""))
    except ValueError:
        return None
    current = today or date.today()
    if born > current:
        return None
    return current.year - born.year - ((current.month, current.day) < (born.month, born.day))


def nation_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    code = text.zfill(2) if text.isdigit() else text
    return NATION_LABELS.get(code, f"民族代码 {text}")


def registration_status(value: Any) -> tuple[str, str]:
    code = str(value or "").strip()
    if code == "0":
        return "active", "未注销"
    if code == "1":
        return "cancelled", "已注销"
    return "unknown", "状态待核对"


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


def _photo_data_url(payload: Any) -> tuple[str, str, str]:
    if not isinstance(payload, dict):
        return "", "error", "photo_invalid_response"
    if _is_authentication_response(payload):
        raise ResidencePlatformError("authentication_expired", "居住证平台登录已失效")
    if payload.get("success") is not True or _business_code(payload.get("code")) != 200:
        return "", "error", "photo_business_error"
    result = payload.get("result")
    encoded = str(result.get("图象数据") or "") if isinstance(result, dict) else ""
    compact = "".join(encoded.split())
    if not compact:
        return "", "missing", ""
    max_encoded_size = ((MAX_PHOTO_BYTES + 2) // 3) * 4
    if len(compact) > max_encoded_size:
        return "", "error", "photo_too_large"
    try:
        content = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return "", "error", "photo_base64_invalid"
    if not content:
        return "", "missing", ""
    if len(content) > MAX_PHOTO_BYTES:
        return "", "error", "photo_too_large"
    if content.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    else:
        return "", "error", "photo_type_invalid"
    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}", "available", ""


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

    async def lookup_detail(self, identity: str) -> ResidenceRegistrationDetail:
        """Read one registered person's whitelisted detail fields and photo."""
        if not self.config.session_ready:
            raise ResidencePlatformError("session_not_ready", "居住证平台尚未登录")
        body = {"sfzh": identity, "xzqh": self.config.organization_code[:6]}
        resident_payload = await self._post_readonly(SEARCH_RESIDENT_PATH, body)
        if _is_authentication_response(resident_payload):
            raise ResidencePlatformError("authentication_expired", "居住证平台登录已失效")
        floating_payload = await self._post_readonly(SEARCH_FLOATING_PATH, body)
        classified = classify_floating_response(floating_payload)
        if classified.error_code == "authentication_expired":
            raise ResidencePlatformError("authentication_expired", "居住证平台登录已失效")
        if classified.state != "registered":
            raise ResidencePlatformError("detail_not_registered", "居住证平台没有可展示的登记资料")
        raw = floating_payload.get("result")
        if not isinstance(raw, dict):
            raise ResidencePlatformError("invalid_response", "居住证平台人员资料响应结构异常")

        birth_date = normalize_birth_date(raw.get("birth"))
        if not birth_date:
            birth_date = normalize_birth_date(identity[6:14])
        status, status_text = registration_status(raw.get("rysfzx"))
        registered_address = "".join(
            part.strip() for part in (
                str(raw.get("jlx_dictText") or ""),
                str(raw.get("mph") or ""),
            ) if part.strip()
        )

        photo_data_url = ""
        photo_state = "missing"
        photo_error_code = ""
        try:
            photo_payload = await self._post_readonly(
                SEARCH_PHOTO_PATH,
                {"sfzh": identity},
            )
            photo_data_url, photo_state, photo_error_code = _photo_data_url(photo_payload)
        except ResidencePlatformError as exc:
            if exc.code == "authentication_expired":
                raise
            photo_state = "error"
            photo_error_code = exc.code if exc.code.startswith("photo_") else "photo_request_error"

        return ResidenceRegistrationDetail(
            birth_date=birth_date,
            age=calculate_age(birth_date),
            ethnicity=nation_label(raw.get("nation")),
            household_area_code=str(raw.get("hjdz") or "").strip(),
            household_detail=str(raw.get("hjdzxz") or "").strip(),
            registered_address=registered_address,
            registration_status=status,
            registration_status_text=status_text,
            updated_at=str(raw.get("rygxsj") or "").strip(),
            photo_data_url=photo_data_url,
            photo_state=photo_state,
            photo_error_code=photo_error_code,
        )

    async def lookup_registration_address(self, identity: str) -> tuple[str, str, str]:
        """Read only the registration state and address; never requests a photo."""
        if not self.config.session_ready:
            raise ResidencePlatformError("session_not_ready", "居住证平台尚未登录")
        body = {"sfzh": identity, "xzqh": self.config.organization_code[:6]}
        resident_payload = await self._post_readonly(SEARCH_RESIDENT_PATH, body)
        if _is_authentication_response(resident_payload):
            raise ResidencePlatformError("authentication_expired", "居住证平台登录已失效")
        floating_payload = await self._post_readonly(SEARCH_FLOATING_PATH, body)
        classified = classify_floating_response(floating_payload)
        if classified.error_code == "authentication_expired":
            raise ResidencePlatformError("authentication_expired", "居住证平台登录已失效")
        raw = floating_payload.get("result") if isinstance(floating_payload, dict) else None
        address = ""
        if isinstance(raw, dict):
            address = "".join(
                part.strip() for part in (
                    str(raw.get("jlx_dictText") or ""),
                    str(raw.get("mph") or ""),
                ) if part.strip()
            )
        return classified.state, address, str(
            (raw or {}).get("rysfzx") or ""
        ) if isinstance(raw, dict) else ""
