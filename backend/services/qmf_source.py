"""Read-only acquisition of pending model-three tasks from the legacy site.

The legacy web page exposes model-three work through ``GET
/api/masses/queryYysList``.  This adapter is deliberately narrower than the
status adapter: it only requests ``judgeType=yys`` and ``hcjg=0`` (未核查),
validates the target station, and returns normalized task rows.  Credentials
remain server-side and no write endpoint is ever called here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import httpx

from config import settings
from services.qmf_community import (
    QmfOrganizationResolution,
    resolve_qmf_organization,
)


MODEL_THREE_PARSER = "疑似未注销模型三"
MODEL_THREE_ENDPOINT = "/api/masses/queryYysList"
QMF_SOURCE_SPREADSHEET_ID = 0
QMF_SOURCE_SHEET_ID = "legacy-model-three"


class QmfSourceError(RuntimeError):
    def __init__(self, code: str, message: str, *, issues: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []


@dataclass(frozen=True)
class QmfSourceConfig:
    base_url: str
    authorization: str
    username: str
    password: str
    login_path: str
    timeout_seconds: int
    max_pages: int
    max_records: int
    police_code: str
    police_name: str

    @property
    def configured(self) -> bool:
        return bool(
            self.base_url
            and self.police_name
            and (
                self.authorization
                or (self.username and self.password and self.login_path)
            )
        )


def source_config() -> QmfSourceConfig:
    configured_base_url = (
        settings.QMF_SOURCE_BASE_URL
        or settings.VISIT_SOURCE_BASE_URL
    ).strip().rstrip("/")
    # The legacy model-three source has its own base URL.  Do not fall back to
    # QMF_API_BASE_URL: that setting belongs to the registration protocol and
    # may point at a different host/path.  Normalize a configured ``/api``
    # suffix because the endpoint constant already includes that prefix.
    if configured_base_url.lower().endswith("/api"):
        configured_base_url = configured_base_url[:-4].rstrip("/")
    return QmfSourceConfig(
        base_url=configured_base_url,
        authorization=(
            settings.VISIT_SOURCE_AUTHORIZATION
            or ""
        ).strip(),
        username=(
            settings.QMF_SOURCE_USERNAME
            or settings.VISIT_SOURCE_USERNAME
            or ""
        ).strip(),
        password=(
            settings.QMF_SOURCE_PASSWORD
            or settings.VISIT_SOURCE_PASSWORD
            or ""
        ),
        login_path="/api/login",
        timeout_seconds=max(1, int(settings.QMF_SOURCE_TIMEOUT_SECONDS or 30)),
        max_pages=max(1, int(settings.QMF_SOURCE_MAX_PAGES or 1000)),
        max_records=max(1, int(settings.QMF_SOURCE_MAX_RECORDS or 100000)),
        police_code=(settings.QMF_EXPECTED_STATION_CODE or settings.VISIT_SOURCE_POLICE_CODE or "").strip(),
        police_name=(settings.QMF_EXPECTED_STATION_NAME or settings.VISIT_SOURCE_POLICE_NAME or "").strip(),
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def _identity(value: Any) -> str:
    return "".join(_text(value).split()).upper()


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "rows", "records", "data", "result"):
            value = data.get(key)
            found = _items(value)
            if found:
                return found
    return []


def _payload(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise QmfSourceError("invalid_json", "模型三来源返回内容不是有效 JSON") from exc
    if not isinstance(payload, dict) or str(payload.get("code", "")) != "200":
        raise QmfSourceError("business_error", "模型三来源返回业务错误")
    return payload.get("data")


def _authorization_value(data: Any) -> str:
    """Extract a token from the legacy login response without logging it."""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in ("token", "accessToken", "access_token", "authorization"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _station_matches(value: Any, expected: str) -> bool:
    actual = "".join(_text(value).split())
    target = "".join(expected.split())
    if not actual or not target:
        return False
    return actual == target or actual.endswith(target)


def normalize_source_row(row: dict[str, Any]) -> dict[str, str]:
    """Map the observed legacy fields into the platform model-three schema."""
    return {
        "截止时间": _text(row.get("endTime") or row.get("deadline") or row.get("jzsj")),
        "核查人": _text(row.get("hcczr") or row.get("inspector") or row.get("checker")),
        "姓名": _text(row.get("xm") or row.get("name")),
        "身份证号": _identity(row.get("sfzh") or row.get("identity")),
        "联系方式": _text(row.get("lxfs") or row.get("phone") or row.get("mobile")),
        "地址": _text(row.get("dz") or row.get("address")),
        "下发社区": _text(row.get("jgmc") or row.get("sqmc") or row.get("community")),
        "核查结果": "",
        "备注": "",
    }


def source_row_key(row: dict[str, str]) -> str:
    identity = row.get("身份证号", "").strip()
    phone = row.get("联系方式", "").strip()
    return sha256(f"{identity}|{phone}".encode("utf-8")).hexdigest()[:32]


async def _request(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> httpx.Response:
    for attempt in range(2):
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            if attempt == 0:
                await asyncio.sleep(0.1)
                continue
            raise QmfSourceError("request_error", "模型三来源请求失败") from exc
        if response.status_code in {502, 503, 504} and attempt == 0:
            await response.aclose()
            await asyncio.sleep(0.1)
            continue
        return response
    raise QmfSourceError("request_error", "模型三来源请求失败")


async def fetch_pending_rows(*, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    config = source_config()
    if not config.configured:
        raise QmfSourceError("not_configured", "模型三来源尚未配置服务器地址或认证信息")

    headers = {"Accept": "application/json"}
    if config.authorization:
        headers["Authorization"] = config.authorization
    raw_rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=config.base_url.rstrip("/"),
        timeout=httpx.Timeout(config.timeout_seconds),
        headers=headers,
        transport=transport,
    ) as client:
        if "Authorization" not in headers:
            response = await _request(
                client,
                "POST",
                config.login_path,
                params={"username": config.username, "password": config.password},
            )
            if response.status_code in {401, 403} or not response.is_success:
                raise QmfSourceError("authentication_failed", "模型三来源认证失败")
            data = _payload(response)
            token = _authorization_value(data)
            if not token:
                raise QmfSourceError("authentication_invalid", "模型三来源认证响应无效")
            client.headers["Authorization"] = token

        fingerprints: set[str] = set()
        for page in range(1, config.max_pages + 1):
            response = await _request(
                client,
                "GET",
                MODEL_THREE_ENDPOINT,
                params={
                    "pageNum": page,
                    "pageSize": 200,
                    "judgeType": "yys",
                    "hcjg": "0",
                    "pcsbm": config.police_code,
                },
            )
            if response.status_code in {401, 403}:
                raise QmfSourceError("forbidden", "模型三来源无权读取")
            if not response.is_success:
                raise QmfSourceError("http_error", f"模型三来源返回 HTTP {response.status_code}")
            data = _payload(response)
            page_rows = _items(data)
            if page_rows:
                fingerprint = sha256(
                    json.dumps(page_rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
                if fingerprint in fingerprints:
                    raise QmfSourceError("pagination_repeated", "模型三来源重复返回同一分页")
                fingerprints.add(fingerprint)
            raw_rows.extend(page_rows)
            if len(raw_rows) > config.max_records:
                raise QmfSourceError("too_many_records", "模型三来源记录数超过保护阈值")
            if len(page_rows) < 200:
                break
        else:
            raise QmfSourceError("too_many_pages", "模型三来源分页超过保护阈值")

    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    for raw in raw_rows:
        if not _station_matches(raw.get("pcsname"), config.police_name):
            issues.append("派出所范围不符")
            continue
        if _text(raw.get("hcjg")) not in {"0", "0.0"}:
            continue
        normalized = normalize_source_row(raw)
        if not normalized["身份证号"]:
            issues.append("身份证号缺失")
            continue
        normalized["__organization_code"] = _text(raw.get("xfsq"))
        normalized["__source_id"] = _text(raw.get("xfid") or raw.get("id"))
        normalized["__source_updated_at"] = _text(raw.get("hcsj") or raw.get("updateTime"))
        rows.append(normalized)
    return {
        "rows": rows,
        "record_count": len(raw_rows),
        "valid_count": len(rows),
        "issue_count": len(issues),
        "issues": sorted(set(issues)),
    }


async def resolve_rows(cur, result: dict[str, Any]) -> dict[str, Any]:
    resolved_rows: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    for row in result.get("rows", []):
        resolution: QmfOrganizationResolution = await resolve_qmf_organization(
            cur,
            organization_code=row.get("__organization_code", ""),
            source_community=row.get("下发社区", ""),
        )
        if resolution.community_id is None:
            unresolved.append({
                "organization_code": resolution.organization_code,
                "source_community": row.get("下发社区", ""),
                "reason": resolution.reason,
            })
            continue
        values = {
            key: value
            for key, value in row.items()
            if not key.startswith("__")
        }
        values["下发社区"] = resolution.community_name
        values["__organization_code"] = resolution.organization_code
        resolved_rows.append(values)
    return {
        **result,
        "rows": resolved_rows,
        "unresolved": unresolved,
        "unresolved_count": len(unresolved),
    }
