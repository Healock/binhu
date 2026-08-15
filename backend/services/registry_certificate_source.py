"""Read-only acquisition of landlord responsibility notices.

The source is the same internal rental-house platform used by visit/rating
acquisition.  This adapter never writes upstream data and only accepts records
owned by the configured police station.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import httpx

from config import settings
from services.visit_source import VisitSourceError, _business_payload, _items, _is_expected_police_station, _text


CERTIFICATE_ENDPOINT = "/api/address/queryHouseCertificate"


async def fetch_certificate_rows() -> dict[str, Any]:
    if not settings.VISIT_SOURCE_BASE_URL:
        raise VisitSourceError("not_configured", "来源平台地址尚未配置")
    headers = {"Accept": "application/json"}
    if settings.VISIT_SOURCE_AUTHORIZATION:
        headers["Authorization"] = settings.VISIT_SOURCE_AUTHORIZATION
    elif not settings.VISIT_SOURCE_USERNAME or not settings.VISIT_SOURCE_PASSWORD:
        raise VisitSourceError("authentication_required", "来源平台认证信息尚未配置")

    rows: list[dict[str, Any]] = []
    rejected = 0
    async with httpx.AsyncClient(
        base_url=settings.VISIT_SOURCE_BASE_URL.rstrip("/"),
        timeout=settings.VISIT_SOURCE_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        if "Authorization" not in headers:
            try:
                response = await client.post(
                    settings.VISIT_SOURCE_LOGIN_PATH,
                    params={"username": settings.VISIT_SOURCE_USERNAME, "password": settings.VISIT_SOURCE_PASSWORD},
                )
                response.raise_for_status()
                payload = response.json()
                token = payload.get("data") if isinstance(payload, dict) and str(payload.get("code", "")) == "200" else None
                if not isinstance(token, str) or not token.strip():
                    raise VisitSourceError("authentication_failed", "来源平台认证失败")
                client.headers["Authorization"] = token.strip()
            except VisitSourceError:
                raise
            except httpx.TimeoutException as exc:
                raise VisitSourceError("timeout", "来源平台认证请求超时") from exc
            except httpx.HTTPStatusError as exc:
                raise VisitSourceError("authentication_failed", "来源平台认证失败") from exc
            except (httpx.RequestError, ValueError) as exc:
                raise VisitSourceError("authentication_failed", "来源平台认证响应无法读取") from exc

        fingerprints: set[str] = set()
        for page in range(1, settings.VISIT_SOURCE_MAX_PAGES + 1):
            try:
                response = await client.get(
                    CERTIFICATE_ENDPOINT,
                    params={
                        "deptCode": settings.VISIT_SOURCE_POLICE_CODE,
                        "pageNum": page,
                        "pageSize": 200,
                    },
                )
                response.raise_for_status()
                payload = _business_payload(response.json())
            except VisitSourceError:
                raise
            except httpx.TimeoutException as exc:
                raise VisitSourceError("timeout", "告知书来源请求超时") from exc
            except httpx.HTTPStatusError as exc:
                code = "forbidden" if exc.response.status_code in (401, 403) else "http_error"
                raise VisitSourceError(code, f"告知书来源返回 HTTP {exc.response.status_code}") from exc
            except (httpx.RequestError, ValueError) as exc:
                raise VisitSourceError("request_error", "告知书来源响应无法读取") from exc

            page_rows = _items(payload)
            if len(page_rows) == 200:
                fingerprint = sha256(json.dumps(page_rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
                if fingerprint in fingerprints:
                    raise VisitSourceError("pagination_repeated", "告知书来源重复返回同一分页，已停止读取")
                fingerprints.add(fingerprint)
            for raw in page_rows:
                station = _text(raw.get("pcsname") or raw.get("policeStation") or raw.get("派出所"))
                if not _is_expected_police_station(station):
                    rejected += 1
                    continue
                address = _text(raw.get("dz") or raw.get("address"))
                community = _text(raw.get("sssq") or raw.get("community"))
                if not address or not community:
                    rejected += 1
                    continue
                row = dict(raw)
                row["pcsname"] = settings.VISIT_SOURCE_POLICE_NAME
                row["address"] = address
                row["community"] = community
                row["source_row"] = len(rows) + 1
                rows.append(row)
            if len(rows) > settings.VISIT_SOURCE_MAX_RECORDS:
                raise VisitSourceError("too_many_records", "告知书来源记录数超过保护阈值")
            if len(page_rows) < 200:
                break
        else:
            raise VisitSourceError("too_many_pages", "告知书来源分页超过保护阈值")

    if not rows:
        raise VisitSourceError("scope_or_schema", "没有通过派出所、社区和地址校验的告知书记录")
    return {"rows": rows, "record_count": len(rows) + rejected, "valid_count": len(rows), "issue_count": rejected}
