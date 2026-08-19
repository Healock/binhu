"""Read-only Peace Code and Manager Code acquisition and aggregation."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any

import httpx

from config import settings
from services.qmf_registration import normalize_identity, valid_identity
from services.visit_source import _is_expected_police_station


SOURCE_META = {
    "peace": {
        "endpoint": "/api/wicket/queryWicketRegisterList",
        "identity": "idCard",
        "time": "comparisonTime",
        "update_fields": ("updateDate",),
        "required": ("idCard", "terminal", "population", "comparisonTime"),
        "station_fields": ("assignJgmc", "jgmc"),
    },
    "manager": {
        "endpoint": "/api/gjm/queryZhUserList",
        "identity": "zhUserIdCard",
        "time": "comparisonTime",
        "update_fields": ("updateDate", "updateTime"),
        "required": ("zhUserIdCard", "gjUserName", "population", "comparisonTime"),
        "station_fields": ("pcsname", "jgmc"),
    },
}

CLASSIFIER_VERSION = "v1"
PAGE_SIZE = 200
INSTRUCTION_RESULTS = {"流口未登记", "流口已注销"}


class CodeSummaryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_label(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).lower()
    return re.sub(r"[\s\u3000,，。．.、;；:：()（）\[\]【】\-—_]+", "", text)


def parse_source_datetime(value: Any, field: str) -> datetime:
    text = _text(value).replace("T", " ").removesuffix("Z")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise CodeSummaryError("schema_changed", f"来源字段 {field} 不是有效时间") from exc


def _stable_id(value: Any) -> tuple[int, int | str]:
    text = _text(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def classify_terminal(
    value: Any,
    *,
    personnel_names: set[str],
    place_names: set[str],
) -> str:
    raw = _text(value)
    normalized = normalize_label(raw)
    if normalized == normalize_label("滨湖所接警大厅"):
        return "dispatch_hall"
    if normalized == normalize_label("苏州湾大厦"):
        return "household_hall"
    person_match = normalized in personnel_names
    place_match = normalized in place_names
    if person_match and place_match:
        return "unclassified"
    if person_match:
        return "patrol"
    if place_match:
        return "social"
    if re.fullmatch(r"[\u4e00-\u9fff]{2}", raw):
        return "patrol"
    if re.fullmatch(r"[\u4e00-\u9fff]{6}", raw):
        return "social"
    return "unclassified"


def _date_rows(start_date: date, end_date: date) -> dict[date, dict[str, int]]:
    result: dict[date, dict[str, int]] = {}
    current = start_date
    while current <= end_date:
        result[current] = {
            "raw_count": 0,
            "total_people": 0,
            "patrol_scan_count": 0,
            "dispatch_hall_scan_count": 0,
            "household_hall_scan_count": 0,
            "social_scan_count": 0,
            "unclassified_scan_count": 0,
            "active_accounts": 0,
            "instruction_count": 0,
            "new_registration_count": 0,
            "excluded_identity_count": 0,
            "duplicate_removed_count": 0,
        }
        current += timedelta(days=1)
    return result


def aggregate_rows(
    kind: str,
    rows: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    *,
    personnel_names: set[str] | None = None,
    place_names: set[str] | None = None,
) -> dict[str, Any]:
    if kind not in SOURCE_META:
        raise CodeSummaryError("invalid_source", "不支持的码数据来源")
    meta = SOURCE_META[kind]
    personnel_names = personnel_names or set()
    place_names = place_names or set()
    daily = _date_rows(start_date, end_date)
    candidates: dict[tuple[date, str], tuple[tuple, dict[str, Any]]] = {}
    valid_rows_by_date: defaultdict[date, int] = defaultdict(int)

    for row in rows:
        if not isinstance(row, dict):
            raise CodeSummaryError("schema_changed", "来源记录结构无效")
        missing_keys = [field for field in meta["required"] if field not in row]
        if missing_keys:
            raise CodeSummaryError("schema_changed", "来源必填字段发生变化")
        station_values = [_text(row.get(field)) for field in meta["station_fields"]]
        if not any(_is_expected_police_station(value) for value in station_values if value):
            raise CodeSummaryError("scope_mismatch", "来源记录不属于滨湖新城派出所")
        business_time = parse_source_datetime(row.get(meta["time"]), meta["time"])
        business_date = business_time.date()
        if business_date < start_date or business_date > end_date:
            raise CodeSummaryError("date_out_of_range", "来源记录超出请求日期范围")
        daily[business_date]["raw_count"] += 1
        identity = normalize_identity(row.get(meta["identity"]))
        if not valid_identity(identity):
            daily[business_date]["excluded_identity_count"] += 1
            continue
        valid_rows_by_date[business_date] += 1
        update_values = tuple(_text(row.get(field)) for field in meta["update_fields"])
        order_key = (business_time, *update_values, _stable_id(row.get("id")))
        key = (business_date, identity)
        existing = candidates.get(key)
        if existing is None or order_key > existing[0]:
            candidates[key] = (order_key, row)

    managers_by_date: defaultdict[date, set[str]] = defaultdict(set)
    for (business_date, _identity), (_order, row) in candidates.items():
        metrics = daily[business_date]
        metrics["total_people"] += 1
        population = _text(row.get("population"))
        if population in INSTRUCTION_RESULTS:
            metrics["instruction_count"] += 1
        if kind == "peace":
            classification = classify_terminal(
                row.get("terminal"),
                personnel_names=personnel_names,
                place_names=place_names,
            )
            metric_key = {
                "patrol": "patrol_scan_count",
                "dispatch_hall": "dispatch_hall_scan_count",
                "household_hall": "household_hall_scan_count",
                "social": "social_scan_count",
                "unclassified": "unclassified_scan_count",
            }[classification]
            metrics[metric_key] += 1
            if population == "流口已登记":
                metrics["new_registration_count"] += 1
        else:
            manager = normalize_label(row.get("gjUserName"))
            if manager:
                managers_by_date[business_date].add(manager)

    for business_date, metrics in daily.items():
        metrics["active_accounts"] = len(managers_by_date[business_date])
        metrics["duplicate_removed_count"] = max(
            0,
            valid_rows_by_date[business_date] - metrics["total_people"],
        )

    canonical = [
        {
            "date": business_date.isoformat(),
            **metrics,
        }
        for business_date, metrics in sorted(daily.items())
    ]
    raw_fingerprints = sorted(
        json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        for row in rows
    )
    source_hash = sha256("\n".join(raw_fingerprints).encode("utf-8")).hexdigest()
    return {
        "kind": kind,
        "rows": canonical,
        "source_hash": source_hash,
        "raw_count": sum(item["raw_count"] for item in canonical),
        "valid_count": sum(item["total_people"] for item in canonical),
        "excluded_count": sum(item["excluded_identity_count"] for item in canonical),
        "duplicate_count": sum(item["duplicate_removed_count"] for item in canonical),
        "unclassified_count": sum(item["unclassified_scan_count"] for item in canonical),
        "classifier_version": CLASSIFIER_VERSION,
    }


async def _authenticate(client: httpx.AsyncClient) -> None:
    if settings.VISIT_SOURCE_AUTHORIZATION:
        client.headers["Authorization"] = settings.VISIT_SOURCE_AUTHORIZATION
        return
    if not settings.VISIT_SOURCE_USERNAME or not settings.VISIT_SOURCE_PASSWORD:
        raise CodeSummaryError("authentication_required", "旧平台认证信息尚未配置")
    try:
        response = await client.post(
            settings.VISIT_SOURCE_LOGIN_PATH,
            params={
                "username": settings.VISIT_SOURCE_USERNAME,
                "password": settings.VISIT_SOURCE_PASSWORD,
            },
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("data") if isinstance(payload, dict) and str(payload.get("code")) == "200" else None
        if not isinstance(token, str) or not token.strip():
            raise CodeSummaryError("authentication_failed", "旧平台认证失败")
        client.headers["Authorization"] = token.strip()
    except CodeSummaryError:
        raise
    except httpx.TimeoutException as exc:
        raise CodeSummaryError("timeout", "旧平台认证请求超时") from exc
    except httpx.HTTPStatusError as exc:
        raise CodeSummaryError("authentication_failed", "旧平台认证失败") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise CodeSummaryError("authentication_failed", "旧平台认证响应无法读取") from exc


async def _fetch_kind(
    client: httpx.AsyncClient,
    kind: str,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    meta = SOURCE_META[kind]
    rows: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    expected_total: int | None = None
    for page in range(1, settings.VISIT_SOURCE_MAX_PAGES + 1):
        params: dict[str, Any] = {
            "startTime": start_date.isoformat(),
            "endTime": end_date.isoformat(),
            "jgbm": settings.VISIT_SOURCE_POLICE_CODE,
            "pageNum": page,
            "pageSize": PAGE_SIZE,
        }
        if kind == "peace":
            params["assignJgbm"] = settings.VISIT_SOURCE_POLICE_CODE
        else:
            params["pcsdm"] = settings.VISIT_SOURCE_POLICE_CODE
        try:
            response = await client.get(meta["endpoint"], params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise CodeSummaryError("timeout", "旧平台码数据请求超时") from exc
        except httpx.HTTPStatusError as exc:
            code = "forbidden" if exc.response.status_code in {401, 403} else "http_error"
            raise CodeSummaryError(code, f"旧平台返回 HTTP {exc.response.status_code}") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise CodeSummaryError("request_error", "旧平台码数据响应无法读取") from exc
        if not isinstance(payload, dict) or str(payload.get("code")) != "200":
            raise CodeSummaryError("upstream_error", "旧平台返回业务错误")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise CodeSummaryError("schema_changed", "旧平台分页结构发生变化")
        try:
            page_total = int(data.get("total", 0))
        except (TypeError, ValueError) as exc:
            raise CodeSummaryError("schema_changed", "旧平台分页总数结构发生变化") from exc
        if page_total < 0:
            raise CodeSummaryError("schema_changed", "旧平台分页总数结构发生变化")
        if expected_total is None:
            expected_total = page_total
            if expected_total > settings.VISIT_SOURCE_MAX_RECORDS:
                raise CodeSummaryError("too_many_records", "旧平台码数据超过记录数保护阈值")
        elif page_total != expected_total:
            raise CodeSummaryError("pagination_changed", "旧平台数据在分页读取期间发生变化")
        page_rows = [item for item in data["list"] if isinstance(item, dict)]
        if len(page_rows) == PAGE_SIZE:
            fingerprint = sha256(
                json.dumps(page_rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if fingerprint in fingerprints:
                raise CodeSummaryError("pagination_repeated", "旧平台重复返回同一分页")
            fingerprints.add(fingerprint)
        rows.extend(page_rows)
        if len(rows) > settings.VISIT_SOURCE_MAX_RECORDS:
            raise CodeSummaryError("too_many_records", "旧平台码数据超过记录数保护阈值")
        if len(page_rows) < PAGE_SIZE:
            if len(rows) != expected_total:
                raise CodeSummaryError("pagination_incomplete", "旧平台分页记录数与总数不一致")
            return rows
    raise CodeSummaryError("too_many_pages", "旧平台码数据超过分页保护阈值")


async def fetch_sources(start_date: date, end_date: date) -> dict[str, Any]:
    if not settings.VISIT_SOURCE_BASE_URL:
        raise CodeSummaryError("not_configured", "旧平台地址尚未配置")
    async with httpx.AsyncClient(
        base_url=settings.VISIT_SOURCE_BASE_URL.rstrip("/"),
        timeout=settings.VISIT_SOURCE_TIMEOUT_SECONDS,
        headers={"Accept": "application/json"},
    ) as client:
        await _authenticate(client)
        results: dict[str, Any] = {}
        for kind in ("peace", "manager"):
            try:
                results[kind] = {"rows": await _fetch_kind(client, kind, start_date, end_date)}
            except CodeSummaryError as exc:
                results[kind] = {"error": exc}
        return results
