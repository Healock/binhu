"""Windows、Android 客户端版本兼容性判断与写请求保护。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from starlette.responses import JSONResponse

from app_version import APP_VERSION
from config import settings


CLIENT_PLATFORM_HEADER = "X-Binhu-Client-Platform"
CLIENT_VERSION_HEADER = "X-Binhu-Client-Version"
NATIVE_CLIENT_PLATFORMS = {"windows", "android"}
SUPPORTED_CLIENT_PLATFORMS = NATIVE_CLIENT_PLATFORMS | {"web"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
WRITE_CHECK_EXEMPT_PATHS = {
    "/api/app/bootstrap",
    "/api/auth/logout",
}

_SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _parse_semver(value: Any) -> tuple[int, int, int, tuple[str, ...] | None] | None:
    text = str(value or "").strip()
    match = _SEMVER_PATTERN.fullmatch(text)
    if not match:
        return None
    prerelease = match.group("prerelease")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        tuple(prerelease.split(".")) if prerelease else None,
    )


def _compare_prerelease(
    left: tuple[str, ...] | None,
    right: tuple[str, ...] | None,
) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return 1
    if right is None:
        return -1
    for left_part, right_part in zip(left, right):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_part) < int(right_part) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_part < right_part else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def compare_semver(left: Any, right: Any) -> int | None:
    """比较两个 SemVer；任一值无效时返回 None。"""
    parsed_left = _parse_semver(left)
    parsed_right = _parse_semver(right)
    if parsed_left is None or parsed_right is None:
        return None
    left_core = parsed_left[:3]
    right_core = parsed_right[:3]
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    return _compare_prerelease(parsed_left[3], parsed_right[3])


def minimum_supported_versions() -> dict[str, str]:
    return {
        "windows": settings.WINDOWS_MIN_SUPPORTED_VERSION.strip(),
        "android": settings.ANDROID_MIN_SUPPORTED_VERSION.strip(),
    }


def evaluate_client_compatibility(
    platform: Any,
    client_version: Any,
    *,
    minimum_versions: Mapping[str, str] | None = None,
    enforcement_enabled: bool | None = None,
    identification_required: bool | None = None,
) -> dict[str, Any]:
    """返回客户端平台、版本和强制升级判断。"""
    normalized_platform = str(platform or "").strip().lower() or None
    normalized_version = str(client_version or "").strip() or None
    versions = dict(minimum_versions or minimum_supported_versions())
    supported = normalized_platform in SUPPORTED_CLIENT_PLATFORMS
    managed = normalized_platform in NATIVE_CLIENT_PLATFORMS
    minimum_version = versions.get(normalized_platform) if managed else None
    comparison = (
        compare_semver(normalized_version, minimum_version)
        if normalized_version and minimum_version
        else None
    )

    reason = None
    must_upgrade = False
    version_valid = _parse_semver(normalized_version) is not None
    if managed:
        if normalized_version is None:
            must_upgrade = True
            reason = "missing_version"
        elif comparison is None:
            must_upgrade = True
            reason = "invalid_version"
        elif comparison < 0:
            must_upgrade = True
            reason = "version_too_old"
    elif normalized_platform is None:
        reason = "missing_platform"
    elif not supported:
        reason = "unsupported_platform"
    elif not version_valid:
        reason = "missing_version" if normalized_version is None else "invalid_version"

    enforcement = (
        settings.CLIENT_WRITE_VERSION_ENFORCEMENT_ENABLED
        if enforcement_enabled is None
        else enforcement_enabled
    )
    require_identification = (
        settings.CLIENT_WRITE_IDENTIFICATION_REQUIRED
        if identification_required is None
        else identification_required
    )
    identification_invalid = require_identification and (
        not supported or not version_valid
    )
    return {
        "platform": normalized_platform,
        "client_version": normalized_version,
        "supported_platform": supported,
        "native_version_policy": managed,
        "minimum_supported_version": minimum_version,
        "must_upgrade": must_upgrade,
        "write_blocked": bool(enforcement and (must_upgrade or identification_invalid)),
        "identification_required": require_identification,
        "reason": reason,
    }


def should_check_write_version(method: str, path: str) -> bool:
    return (
        method.upper() in WRITE_METHODS
        and path.startswith("/api/")
        and path not in WRITE_CHECK_EXEMPT_PATHS
    )


class ClientCompatibilityMiddleware:
    """统一阻止过旧客户端，以及启用身份必填后的无标识写请求。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not should_check_write_version(
            scope.get("method", "GET"),
            scope.get("path", ""),
        ):
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        compatibility = evaluate_client_compatibility(
            headers.get(CLIENT_PLATFORM_HEADER.lower()),
            headers.get(CLIENT_VERSION_HEADER.lower()),
        )
        if not compatibility["write_blocked"]:
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=426,
            content={
                "detail": {
                    "code": "client_upgrade_required",
                    "message": (
                        "客户端版本过旧，请升级后继续操作"
                        if compatibility["must_upgrade"]
                        else "客户端版本信息缺失或不受支持，请更新客户端后继续操作"
                    ),
                    "server_version": APP_VERSION,
                    "compatibility": compatibility,
                }
            },
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
