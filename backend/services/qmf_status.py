"""Read-only reconciliation against the legacy 全民防 management platform.

This adapter deliberately uses only the management-side login and exact
single-person status query.  It never exposes an arbitrary URL or request body
and never calls any mobile registration/write endpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from config import settings
from services.qmf_registration import (
    RESULT_IN_WU,
    RESULT_LEAVE_NOT_RETURNING,
    RESULT_RECENT_RETURN,
    normalize_identity,
    normalize_qmf_result,
)


STATUS_PENDING = "pending"
STATUS_COMPLETED_MATCH = "completed_match"
STATUS_COMPLETED_MISMATCH = "completed_mismatch"
STATUS_NOT_FOUND = "not_found"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_STATION_MISMATCH = "station_mismatch"
STATUS_UNKNOWN_RESULT = "unknown_result"
STATUS_UNAVAILABLE = "unavailable"

_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})
_MAX_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 0.1


@dataclass(frozen=True)
class QmfStatusConfig:
    base_url: str
    authorization: str
    username: str
    password: str
    login_path: str
    timeout_seconds: int
    expected_station_name: str

    @property
    def configured(self) -> bool:
        return bool(
            self.base_url
            and (
                self.authorization
                or (self.username and self.password and self.login_path)
            )
            and self.expected_station_name
        )


@dataclass(frozen=True)
class QmfLegacyStatus:
    state: str
    result: str = ""
    result_text: str = ""
    checked_at: str = ""
    station: str = ""
    matches_platform_result: bool | None = None
    origin: str = "legacy_manual_or_other"
    reason: str = ""

    def public_payload(self) -> dict[str, Any]:
        return asdict(self)


def settings_status_config() -> QmfStatusConfig:
    return QmfStatusConfig(
        base_url=str(settings.VISIT_SOURCE_BASE_URL or "").strip(),
        authorization=str(settings.VISIT_SOURCE_AUTHORIZATION or "").strip(),
        username=str(settings.VISIT_SOURCE_USERNAME or "").strip(),
        password=str(settings.VISIT_SOURCE_PASSWORD or ""),
        login_path=str(settings.VISIT_SOURCE_LOGIN_PATH or "/api/login").strip(),
        timeout_seconds=max(1, int(settings.VISIT_SOURCE_TIMEOUT_SECONDS or 30)),
        expected_station_name=str(settings.VISIT_SOURCE_POLICE_NAME or "").strip(),
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_station(value: Any) -> str:
    return "".join(_text(value).split())


def _business_payload(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(payload, dict) or str(payload.get("code", "")) != "200":
        raise ValueError("business_error")
    return payload.get("data")


def normalize_legacy_result(code: Any, text: Any) -> tuple[str, str]:
    """Return ``(state, canonical_result)`` without echoing unknown text.

    The management endpoint exposes both a numeric result and its display
    text.  Parse them independently so ``近期返吴(不注销)`` is never mistaken
    for a logout merely because it contains the word ``注销``.  If both fields
    are recognized but disagree, stop safely instead of choosing one.
    """
    code_text = _text(code)
    result_text = _text(text).replace(" ", "")

    code_result = {
        "0": (STATUS_PENDING, ""),
        "1": ("completed", RESULT_LEAVE_NOT_RETURNING),
        "2": ("completed", RESULT_IN_WU),
        "3": ("completed", RESULT_RECENT_RETURN),
    }.get(code_text)
    text_result: tuple[str, str] | None = None
    if "未核查" in result_text or "待核查" in result_text:
        text_result = (STATUS_PENDING, "")
    elif any(
        token in result_text for token in (RESULT_RECENT_RETURN, "近期反吴")
    ):
        text_result = ("completed", RESULT_RECENT_RETURN)
    elif (
        result_text == RESULT_IN_WU
        or result_text.startswith(f"{RESULT_IN_WU}(")
        or result_text.startswith(f"{RESULT_IN_WU}（")
    ):
        text_result = ("completed", RESULT_IN_WU)
    elif (
        any(token in result_text for token in ("离开不返吴", "离吴"))
        or ("注销" in result_text and "不注销" not in result_text)
    ):
        text_result = ("completed", RESULT_LEAVE_NOT_RETURNING)

    if code_result and text_result and code_result != text_result:
        return STATUS_UNKNOWN_RESULT, ""
    if text_result:
        return text_result
    if code_result:
        return code_result
    return STATUS_UNKNOWN_RESULT, ""


class QmfLegacyStatusClient:
    def __init__(
        self,
        *,
        config: QmfStatusConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._config = config or settings_status_config()
        self._transport = transport

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.request(method, path, **kwargs)
            except httpx.RequestError:
                if attempt + 1 < _MAX_ATTEMPTS:
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                raise
            if (
                response.status_code in _RETRYABLE_HTTP_STATUS
                and attempt + 1 < _MAX_ATTEMPTS
            ):
                await response.aclose()
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            return response
        raise RuntimeError("unreachable")

    async def query(
        self,
        *,
        identity: str,
        expected_result: str,
    ) -> QmfLegacyStatus:
        normalized_identity = normalize_identity(identity)
        normalized_expected = normalize_qmf_result(expected_result)
        if not self._config.configured:
            return QmfLegacyStatus(
                state=STATUS_UNAVAILABLE,
                reason="全民防管理端状态查询尚未配置",
            )
        if not normalized_identity or not normalized_expected:
            return QmfLegacyStatus(
                state=STATUS_UNAVAILABLE,
                reason="平台任务状态不完整",
            )

        headers = {"Accept": "application/json"}
        if self._config.authorization:
            headers["Authorization"] = self._config.authorization
        try:
            async with httpx.AsyncClient(
                base_url=self._config.base_url.rstrip("/"),
                timeout=httpx.Timeout(self._config.timeout_seconds),
                headers=headers,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                if "Authorization" not in client.headers:
                    login_response = await self._send(
                        client,
                        "POST",
                        self._config.login_path,
                        params={
                            "username": self._config.username,
                            "password": self._config.password,
                        },
                    )
                    if login_response.status_code in {401, 403}:
                        return QmfLegacyStatus(
                            state=STATUS_UNAVAILABLE,
                            reason="全民防管理端认证失败",
                        )
                    if not login_response.is_success:
                        return QmfLegacyStatus(
                            state=STATUS_UNAVAILABLE,
                            reason="全民防管理端认证暂时不可用",
                        )
                    token = _business_payload(login_response)
                    if not isinstance(token, str) or not token.strip():
                        return QmfLegacyStatus(
                            state=STATUS_UNAVAILABLE,
                            reason="全民防管理端认证响应无效",
                        )
                    client.headers["Authorization"] = token.strip()

                response = await self._send(
                    client,
                    "GET",
                    "/api/masses/queryYysList",
                    params={
                        "pageNum": "1",
                        "pageSize": "20",
                        "judgeType": "yys",
                        "sfzh": normalized_identity,
                    },
                )
                if response.status_code in {401, 403}:
                    return QmfLegacyStatus(
                        state=STATUS_UNAVAILABLE,
                        reason="全民防管理端状态查询无权限",
                    )
                if not response.is_success:
                    return QmfLegacyStatus(
                        state=STATUS_UNAVAILABLE,
                        reason="全民防管理端状态查询暂时不可用",
                    )
                data = _business_payload(response)
        except (httpx.RequestError, ValueError, TypeError):
            return QmfLegacyStatus(
                state=STATUS_UNAVAILABLE,
                reason="全民防管理端状态暂时无法确认",
            )

        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            return QmfLegacyStatus(
                state=STATUS_UNAVAILABLE,
                reason="全民防管理端状态响应结构已变化",
            )
        try:
            total = int(data.get("total", -1))
        except (TypeError, ValueError):
            return QmfLegacyStatus(
                state=STATUS_UNAVAILABLE,
                reason="全民防管理端状态响应结构已变化",
            )
        matching = [
            row
            for row in data["list"]
            if isinstance(row, dict)
            and normalize_identity(row.get("sfzh")) == normalized_identity
        ]
        if total == 0 and not data["list"]:
            return QmfLegacyStatus(
                state=STATUS_NOT_FOUND,
                reason="管理端未查到记录，将继续由手机待办接口复核",
            )
        if total > 1 or len(matching) > 1:
            return QmfLegacyStatus(
                state=STATUS_AMBIGUOUS,
                reason="全民防管理端存在多条匹配记录",
            )
        if total != 1 or len(matching) != 1:
            return QmfLegacyStatus(
                state=STATUS_UNAVAILABLE,
                reason="全民防管理端状态响应与查询条件不一致",
            )

        row = matching[0]
        station = _text(row.get("pcsname"))[:200]
        checked_at = _text(row.get("hcsj"))[:64]
        if _normalized_station(station) != _normalized_station(
            self._config.expected_station_name
        ):
            return QmfLegacyStatus(
                state=STATUS_STATION_MISMATCH,
                station=station,
                reason="全民防管理端记录不属于目标派出所",
            )

        result_state, result = normalize_legacy_result(
            row.get("hcjg"), row.get("hcjgtext")
        )
        if result_state == STATUS_PENDING:
            return QmfLegacyStatus(
                state=STATUS_PENDING,
                result_text="未核查",
                checked_at=checked_at,
                station=station,
                matches_platform_result=None,
            )
        if result_state == STATUS_UNKNOWN_RESULT:
            return QmfLegacyStatus(
                state=STATUS_UNKNOWN_RESULT,
                result_text="未知结果",
                checked_at=checked_at,
                station=station,
                reason="全民防管理端返回了未支持的核查结果",
            )
        matches = result == normalized_expected
        return QmfLegacyStatus(
            state=STATUS_COMPLETED_MATCH if matches else STATUS_COMPLETED_MISMATCH,
            result=result,
            result_text=result,
            checked_at=checked_at,
            station=station,
            matches_platform_result=matches,
            reason=(
                "全民防已反馈，无需重复登记"
                if matches
                else "全民防反馈结果与平台核查结果不一致"
            ),
        )


def ensure_registration_allowed(status: QmfLegacyStatus) -> None:
    """Raise a safe pre-write error unless another interface must continue."""
    from services.qmf_registration import QmfPreviewError

    if status.state in {STATUS_PENDING, STATUS_NOT_FOUND}:
        return
    mapping = {
        STATUS_COMPLETED_MATCH: (
            "legacy_already_completed",
            "全民防已反馈，无需重复登记",
            409,
        ),
        STATUS_COMPLETED_MISMATCH: (
            "legacy_result_mismatch",
            "全民防已反馈，但结果与平台核查结果不一致，请人工核对",
            409,
        ),
        STATUS_AMBIGUOUS: (
            "legacy_status_ambiguous",
            "全民防存在多条匹配记录，请人工核对",
            409,
        ),
        STATUS_STATION_MISMATCH: (
            "legacy_station_mismatch",
            "全民防记录不属于目标派出所，请人工核对",
            403,
        ),
        STATUS_UNKNOWN_RESULT: (
            "legacy_result_unknown",
            "全民防核查结果无法识别，请人工核对",
            409,
        ),
        STATUS_UNAVAILABLE: (
            "legacy_status_unavailable",
            "全民防反馈状态暂时无法确认，已停止登记",
            503,
        ),
    }
    code, message, http_status = mapping.get(
        status.state,
        (
            "legacy_status_invalid",
            "全民防反馈状态无法确认，已停止登记",
            503,
        ),
    )
    raise QmfPreviewError(code, message, http_status, step="legacy_status")
