"""Read-only acquisition of landlord responsibility notices.

The source is the same internal rental-house platform used by visit/rating
acquisition.  This adapter never writes upstream data and only accepts records
owned by the configured police station.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

from config import settings
from services.visit_source import VisitSourceError, _business_payload, _items, _is_expected_police_station, _text


CERTIFICATE_ENDPOINT = "/api/address/queryHouseCertificate"
CERTIFICATE_PAGE_SIZE = 200
CERTIFICATE_IMAGE_MAX_BYTES = 10 * 1024 * 1024
CERTIFICATE_IMAGE_REF = re.compile(
    r"^\d{4}-\d{2}-\d{2}/[^/\\?#:]{1,180}\.(?:jpe?g|png)$",
    re.IGNORECASE,
)


def _stable_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u3000", " ").split())


def normalize_certificate_image_ref(value: Any) -> str:
    reference = str(value or "").strip()
    if not CERTIFICATE_IMAGE_REF.fullmatch(reference):
        raise VisitSourceError("invalid_image_reference", "告知书图片引用格式无效")
    return reference


def _image_type(content: bytes) -> tuple[str, str] | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    return None


async def fetch_certificate_image(value: Any) -> tuple[bytes, str, str]:
    reference = normalize_certificate_image_ref(value)
    if not settings.CERTIFICATE_IMAGE_BASE_URL:
        raise VisitSourceError("image_not_configured", "告知书图片来源尚未配置")
    url = (
        settings.CERTIFICATE_IMAGE_BASE_URL.rstrip("/")
        + "/"
        + quote(reference, safe="/-_.~")
    )
    try:
        async with httpx.AsyncClient(
            timeout=settings.VISIT_SOURCE_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"Accept": "image/jpeg,image/png"},
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code == 404:
                    raise VisitSourceError("image_not_found", "来源平台中没有这张告知书图片")
                if 300 <= response.status_code < 400:
                    raise VisitSourceError("image_redirected", "告知书图片来源返回了异常跳转")
                response.raise_for_status()
                try:
                    content_length = int(response.headers.get("content-length", "0") or 0)
                except (TypeError, ValueError):
                    content_length = 0
                if content_length > CERTIFICATE_IMAGE_MAX_BYTES:
                    raise VisitSourceError("image_too_large", "告知书图片超过大小限制")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > CERTIFICATE_IMAGE_MAX_BYTES:
                        raise VisitSourceError("image_too_large", "告知书图片超过大小限制")
    except VisitSourceError:
        raise
    except httpx.TimeoutException as exc:
        raise VisitSourceError("image_timeout", "告知书图片读取超时") from exc
    except httpx.HTTPStatusError as exc:
        raise VisitSourceError(
            "image_http_error",
            f"告知书图片来源返回 HTTP {exc.response.status_code}",
        ) from exc
    except httpx.RequestError as exc:
        raise VisitSourceError("image_request_error", "告知书图片来源暂时无法访问") from exc
    detected = _image_type(bytes(content))
    if not detected:
        raise VisitSourceError("invalid_image", "告知书图片内容格式无效")
    media_type, extension = detected
    return bytes(content), media_type, extension


def certificate_source_ref(row: dict[str, Any]) -> str:
    """Return a stable, non-sensitive identity for one upstream notice."""
    identity_fields = (
        ("id", row.get("id")),
        ("document", row.get("documentid") or row.get("documentId")),
        ("notice", row.get("dztzm")),
    )
    for prefix, value in identity_fields:
        normalized = _stable_text(value)
        if normalized:
            digest = sha256(normalized.encode("utf-8")).hexdigest()
            return f"certificate:{prefix}:{digest}"
    fallback = {
        "community": _stable_text(row.get("community") or row.get("sssq")),
        "address": _stable_text(row.get("address") or row.get("dz")),
        "landlord": _stable_text(row.get("czrxm") or row.get("landlord_name")),
        "landlord_id": _stable_text(row.get("czrzjhm") or row.get("landlord_identity_number")),
        "renter": _stable_text(row.get("sjczrxm") or row.get("actual_renter_name")),
        "renter_id": _stable_text(row.get("sjczrzjhm") or row.get("actual_renter_identity_number")),
    }
    digest = sha256(
        json.dumps(fallback, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"certificate:fallback:{digest}"


def certificate_content_hash(row: dict[str, Any]) -> str:
    comparable = {
        str(key): value
        for key, value in row.items()
        if str(key) not in {"source_row", "source_ref", "source_content_hash"}
    }
    return sha256(
        json.dumps(comparable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def certificate_page_fingerprint(rows: list[dict[str, Any]]) -> str:
    return sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def normalize_certificate_page(
    page_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    rejected = 0
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
        row["source_ref"] = certificate_source_ref(row)
        row["source_content_hash"] = certificate_content_hash(row)
        rows.append(row)
    return rows, rejected


async def iter_certificate_pages(
    *,
    start_page: int = 1,
) -> AsyncIterator[dict[str, Any]]:
    """Yield validated upstream pages without keeping the full source in memory."""
    if start_page < 1:
        raise ValueError("start_page must be positive")
    if not settings.VISIT_SOURCE_BASE_URL:
        raise VisitSourceError("not_configured", "来源平台地址尚未配置")
    headers = {"Accept": "application/json"}
    if settings.VISIT_SOURCE_AUTHORIZATION:
        headers["Authorization"] = settings.VISIT_SOURCE_AUTHORIZATION
    elif not settings.VISIT_SOURCE_USERNAME or not settings.VISIT_SOURCE_PASSWORD:
        raise VisitSourceError("authentication_required", "来源平台认证信息尚未配置")

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

        for page in range(start_page, settings.VISIT_SOURCE_MAX_PAGES + 1):
            try:
                response = await client.get(
                    CERTIFICATE_ENDPOINT,
                    params={
                        "deptCode": settings.VISIT_SOURCE_POLICE_CODE,
                        "pageNum": page,
                        "pageSize": CERTIFICATE_PAGE_SIZE,
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
            normalized, rejected = normalize_certificate_page(page_rows)
            yield {
                "page": page,
                "raw_count": len(page_rows),
                "rows": normalized,
                "rejected_count": rejected,
                "fingerprint": certificate_page_fingerprint(page_rows),
                "is_last": len(page_rows) < CERTIFICATE_PAGE_SIZE,
            }
            if len(page_rows) < CERTIFICATE_PAGE_SIZE:
                return
    raise VisitSourceError("too_many_pages", "告知书来源分页超过保护阈值")


async def fetch_certificate_rows() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    rejected = 0
    fingerprints: set[str] = set()
    total_records = 0
    async for page in iter_certificate_pages():
        fingerprint = str(page["fingerprint"])
        if page["raw_count"] == CERTIFICATE_PAGE_SIZE and fingerprint in fingerprints:
            raise VisitSourceError("pagination_repeated", "告知书来源重复返回同一分页，已停止读取")
        fingerprints.add(fingerprint)
        for row in page["rows"]:
            materialized = dict(row)
            materialized["source_row"] = len(rows) + 1
            rows.append(materialized)
        rejected += int(page["rejected_count"])
        total_records += int(page["raw_count"])
        if total_records > settings.VISIT_SOURCE_MAX_RECORDS:
            raise VisitSourceError("too_many_records", "告知书来源记录数超过保护阈值")

    if not rows:
        raise VisitSourceError("scope_or_schema", "没有通过派出所、社区和地址校验的告知书记录")
    return {"rows": rows, "record_count": total_records, "valid_count": len(rows), "issue_count": rejected}
