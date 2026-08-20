"""多端会话的设备识别与安全摘要工具。"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Literal

from config import settings

DeviceType = Literal["desktop", "mobile"]

_MOBILE_RE = re.compile(
    r"android|iphone|ipad|ipod|mobile|windows phone|webos|blackberry",
    re.IGNORECASE,
)


def normalize_device_type(value: object) -> DeviceType | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"mobile", "phone", "tablet"}:
        return "mobile"
    if normalized in {"desktop", "web", "computer"}:
        return "desktop"
    return None


def infer_device_type(
    *,
    requested: object = None,
    platform_header: object = None,
    user_agent: object = None,
    mobile_hint: object = None,
) -> DeviceType:
    """从客户端声明和请求头推断端类型。

    这些信息只用于会话分槽，不能作为认证依据。冲突时优先采用明确的
    mobile hint，其次采用请求头声明，最后按 User-Agent 判断；完全未知时
    安全降级到电脑端。
    """
    requested_type = normalize_device_type(requested)
    platform_type = normalize_device_type(platform_header)
    hint = str(mobile_hint or "").strip().lower()
    if hint in {"?1", "1", "true", "yes"}:
        return "mobile"
    if hint in {"?0", "0", "false", "no"}:
        return "desktop"
    if requested_type and platform_type and requested_type != platform_type:
        return platform_type
    if platform_type:
        return platform_type
    if requested_type:
        return requested_type
    return "mobile" if _MOBILE_RE.search(str(user_agent or "")) else "desktop"


def hash_device_id(device_id: object) -> str | None:
    value = str(device_id or "").strip()
    if not value or len(value) > 128:
        return None
    key = str(settings.ENCRYPTION_KEY or "binhu-session-device").encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def user_agent_family(user_agent: object) -> str:
    value = str(user_agent or "").lower()
    if "edg/" in value:
        return "Edge"
    if "firefox/" in value:
        return "Firefox"
    if "chrome/" in value and "chromium" not in value:
        return "Chrome"
    if "safari/" in value and "chrome/" not in value:
        return "Safari"
    if "micromessenger" in value:
        return "微信"
    if "android" in value:
        return "Android 浏览器"
    if "iphone" in value or "ipad" in value:
        return "iOS 浏览器"
    return "其他浏览器"


def public_session_fingerprint(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:12]
