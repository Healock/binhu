"""Read-only acquisition adapter for the internal visit/rating platform.

The external response contract is intentionally configurable. Until a real
redacted response is supplied, production access stays disabled and tests use
the deterministic mock transport below.
"""

from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from typing import Any

import httpx
from openpyxl import Workbook

from config import settings
from services.star_rating_import import STAR_RATING_HEADERS
from services.visit_import import VISIT_HEADERS


class VisitSourceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


SOURCE_META = {
    "detail": {
        "endpoint": "/api/enterHouse/queryEnterHouseClockInList",
        "page": "走访明细",
        "headers": VISIT_HEADERS,
    },
    "rating": {
        "endpoint": "/api/starHouse/queryStarHouseList",
        "page": "新星级评分管理",
        "headers": STAR_RATING_HEADERS,
    },
}

REQUIRED_HEADERS = {
    "detail": (
        VISIT_HEADERS[1], VISIT_HEADERS[2], VISIT_HEADERS[3],
        VISIT_HEADERS[4], VISIT_HEADERS[6], VISIT_HEADERS[7],
        VISIT_HEADERS[8], VISIT_HEADERS[9], VISIT_HEADERS[10],
    ),
    "rating": (
        STAR_RATING_HEADERS[1], STAR_RATING_HEADERS[2],
        STAR_RATING_HEADERS[3], STAR_RATING_HEADERS[4],
        STAR_RATING_HEADERS[5],
    ),
}

DEFAULT_ALIASES = {
    "detail": {
        VISIT_HEADERS[0]: ["pcsname", "policeStation", "police_station", VISIT_HEADERS[0]],
        VISIT_HEADERS[1]: ["community", "communityName", "village", VISIT_HEADERS[1]],
        VISIT_HEADERS[2]: ["entryMethod", "enterType", VISIT_HEADERS[2]],
        VISIT_HEADERS[3]: ["address", "dz", "住址", VISIT_HEADERS[3]],
        VISIT_HEADERS[4]: ["operatorName", "trueName", "gridName", VISIT_HEADERS[4]],
        VISIT_HEADERS[5]: ["operatorAccount", "account", "idCard", VISIT_HEADERS[5]],
        VISIT_HEADERS[6]: ["visitAt", "enterTime", "clockInTime", "走访时间", VISIT_HEADERS[6]],
        VISIT_HEADERS[7]: ["roomCheckCount", "roomCount", "checkCount", VISIT_HEADERS[7]],
        VISIT_HEADERS[8]: ["addedCount", "addCount", VISIT_HEADERS[8]],
        VISIT_HEADERS[9]: ["changedCount", "changeCount", VISIT_HEADERS[9]],
        VISIT_HEADERS[10]: ["cancelledCount", "cancelCount", "注销", VISIT_HEADERS[10]],
    },
    "rating": {
        STAR_RATING_HEADERS[0]: ["pcsname", "policeStation", STAR_RATING_HEADERS[0]],
        STAR_RATING_HEADERS[1]: ["community", "communityName", "village", STAR_RATING_HEADERS[1]],
        STAR_RATING_HEADERS[2]: ["address", "dz", "住址", STAR_RATING_HEADERS[2]],
        STAR_RATING_HEADERS[3]: ["score", "得分", STAR_RATING_HEADERS[3]],
        STAR_RATING_HEADERS[4]: ["starLevel", "houseLevel", "level", STAR_RATING_HEADERS[4]],
        STAR_RATING_HEADERS[5]: ["collectedAt", "createTime", "createtime", STAR_RATING_HEADERS[5]],
        STAR_RATING_HEADERS[6]: ["hazardDetails", "hiddenDanger", "remark", STAR_RATING_HEADERS[6]],
    },
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "rows", "records", "list", "result"):
        value = payload.get(key)
        found = _items(value)
        if found:
            return found
    return []


def _configured_aliases(kind: str) -> dict[str, list[str]]:
    raw = getattr(settings, f"VISIT_SOURCE_{kind.upper()}_FIELD_MAP", "")
    if not raw:
        if not settings.VISIT_SOURCE_MOCK:
            raise VisitSourceError(
                "field_map_required",
                "尚未配置经确认的来源字段映射，已阻止读取生产响应",
            )
        return DEFAULT_ALIASES[kind]
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return {key: [str(item) for item in values] for key, values in value.items()}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VisitSourceError("invalid_field_map", "来源字段映射配置不是有效 JSON") from exc


def _value(row: dict[str, Any], aliases: list[str]) -> str:
    for alias in aliases:
        if alias in row and row[alias] not in (None, ""):
            return _text(row[alias])
    return ""


def _mock_rows(kind: str) -> list[dict[str, Any]]:
    if kind == "detail":
        return [{
            VISIT_HEADERS[0]: settings.VISIT_SOURCE_POLICE_NAME,
            VISIT_HEADERS[1]: "测试社区",
            VISIT_HEADERS[2]: "扫码",
            VISIT_HEADERS[3]: "测试路1号",
            VISIT_HEADERS[4]: "测试网格员",
            VISIT_HEADERS[5]: "mock-account",
            VISIT_HEADERS[6]: "2026-08-13 09:00:00",
            VISIT_HEADERS[7]: 1,
            VISIT_HEADERS[8]: 1,
            VISIT_HEADERS[9]: 0,
            VISIT_HEADERS[10]: 0,
        }]
    return [{
        STAR_RATING_HEADERS[0]: settings.VISIT_SOURCE_POLICE_NAME,
        STAR_RATING_HEADERS[1]: "测试社区",
        STAR_RATING_HEADERS[2]: "测试路1号",
        STAR_RATING_HEADERS[3]: 80,
        STAR_RATING_HEADERS[4]: "一星出租房",
        STAR_RATING_HEADERS[5]: "2026-08-13 09:00:00",
        STAR_RATING_HEADERS[6]: "",
    }]


async def fetch_rows(kind: str, start_date: date, end_date: date) -> dict[str, Any]:
    if kind not in SOURCE_META:
        raise VisitSourceError("invalid_source", "不支持的数据来源")
    response_business_date = None
    if settings.VISIT_SOURCE_MOCK:
        raw_rows = _mock_rows(kind)
    elif not settings.VISIT_SOURCE_BASE_URL:
        raise VisitSourceError("not_configured", "来源平台地址尚未配置，当前仅支持脱敏模拟数据")
    else:
        headers = {"Accept": "application/json"}
        if settings.VISIT_SOURCE_AUTHORIZATION:
            headers["Authorization"] = settings.VISIT_SOURCE_AUTHORIZATION
        raw_rows = []
        endpoint = SOURCE_META[kind]["endpoint"]
        async with httpx.AsyncClient(
            base_url=settings.VISIT_SOURCE_BASE_URL.rstrip("/"),
            timeout=settings.VISIT_SOURCE_TIMEOUT_SECONDS,
            headers=headers,
        ) as client:
            for page in range(1, settings.VISIT_SOURCE_MAX_PAGES + 1):
                try:
                    response = await client.get(endpoint, params={
                        "deptCode": settings.VISIT_SOURCE_POLICE_CODE,
                        "startTime": start_date.isoformat(),
                        "endTime": end_date.isoformat(),
                        "pageNum": page,
                        "pageSize": 200,
                    })
                    response.raise_for_status()
                    payload = response.json()
                    if isinstance(payload, dict):
                        for key in ("businessDate", "business_date", "dataDate", "statDate", "date"):
                            value = payload.get(key)
                            if value:
                                response_business_date = _text(value)[:10]
                                break
                except httpx.TimeoutException as exc:
                    raise VisitSourceError("timeout", "来源平台请求超时") from exc
                except httpx.HTTPStatusError as exc:
                    code = "forbidden" if exc.response.status_code in (401, 403) else "http_error"
                    raise VisitSourceError(code, f"来源平台返回 HTTP {exc.response.status_code}") from exc
                except (httpx.RequestError, ValueError) as exc:
                    raise VisitSourceError("request_error", "来源平台响应无法读取") from exc
                page_rows = _items(payload)
                raw_rows.extend(page_rows)
                if len(raw_rows) > settings.VISIT_SOURCE_MAX_RECORDS:
                    raise VisitSourceError("too_many_records", "来源记录数超过保护阈值")
                if len(page_rows) < 200:
                    break
            else:
                raise VisitSourceError("too_many_pages", "来源分页超过保护阈值")

    if not raw_rows:
        raise VisitSourceError("empty", "来源平台返回空数据")
    aliases = _configured_aliases(kind)
    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    for row in raw_rows:
        normalized = {header: _value(row, aliases.get(header, [header])) for header in SOURCE_META[kind]["headers"]}
        if normalized[SOURCE_META[kind]["headers"][0]] != settings.VISIT_SOURCE_POLICE_NAME:
            issues.append("派出所范围不符")
            continue
        missing = [header for header in REQUIRED_HEADERS[kind] if not normalized[header]]
        if missing:
            issues.append("必填字段缺失")
            continue
        rows.append(normalized)
    if not rows:
        raise VisitSourceError("scope_or_schema", issues[0] if issues else "没有通过字段和派出所校验的记录")
    return {
        "kind": kind,
        "rows": rows,
        "record_count": len(raw_rows),
        "valid_count": len(rows),
        "issue_count": len(issues),
        "issues": sorted(set(issues)),
        "response_business_date": response_business_date or start_date.isoformat(),
    }


def workbook_bytes(kind: str, rows: list[dict[str, Any]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    headers = SOURCE_META[kind]["headers"]
    sheet.append(list(headers))
    for row in rows:
        sheet.append([row.get(header, "") for header in headers])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def safe_payload(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str)


async def preview_diff(
    conn,
    *,
    kind: str,
    rows: list[dict[str, Any]],
    timezone_name: str,
) -> dict[str, int]:
    """Calculate a read-only diff using the same parser and matching rules."""
    import asyncio

    content = workbook_bytes(kind, rows)
    if kind == "detail":
        from services.visit_import import (
            _load_existing_rows,
            decide_existing_action,
            parse_visit_workbook,
        )

        parsed = await asyncio.to_thread(parse_visit_workbook, content, timezone_name)
        existing = await _load_existing_rows(conn, [row.row_key for row in parsed.rows])
        actions = [decide_existing_action(existing.get(row.row_key), row) for row in parsed.rows]
        incoming_keys = {row.row_key for row in parsed.rows}
        deleted = 0
        if parsed.start_date and parsed.end_date:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT `_row_key` FROM t_visit_details WHERE `业务日期` BETWEEN %s AND %s",
                    (parsed.start_date, parsed.end_date),
                )
                deleted = sum(1 for row in await cur.fetchall() if row[0] not in incoming_keys)
        return {
            "inserted": actions.count("insert"),
            "updated": actions.count("update"),
            "unchanged": actions.count("unchanged"),
            "deleted": deleted,
            "unmatched": 0,
            "ambiguous": 0,
        }

    from services.star_rating_import import (
        _load_visit_candidates,
        choose_star_rating_matches,
        parse_star_rating_workbook,
    )

    parsed = await asyncio.to_thread(parse_star_rating_workbook, content, timezone_name)
    candidates = await _load_visit_candidates(
        conn,
        sorted({row.address_key for row in parsed.rows}),
    )
    matches, _, unmatched, ambiguous = choose_star_rating_matches(parsed.rows, candidates)
    inserted = 0
    updated = 0
    unchanged = 0
    for match in matches:
        incoming = match.rating.star_values(
            canonical_community=match.rating.community,
            time_difference_seconds=match.time_difference_seconds,
        )
        if match.visit.existing_star_values is None:
            inserted += 1
        elif match.visit.existing_star_values == incoming[:-1]:
            unchanged += 1
        else:
            updated += 1
    matched_ids = {match.visit.id for match in matches}
    deleted = 0
    if parsed.start_date and parsed.end_date:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM t_visit_details WHERE `星级采集日期` BETWEEN %s AND %s",
                (parsed.start_date, parsed.end_date),
            )
            deleted = sum(1 for row in await cur.fetchall() if row[0] not in matched_ids)
    return {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "deleted": deleted,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
    }


async def commit_rows(
    conn,
    *,
    kind: str,
    rows: list[dict[str, Any]],
    start_date: date,
    user_id: int,
    replace_range: bool = True,
    source_type: str = "manual_source",
    source_run_id: int | None = None,
) -> dict[str, Any]:
    """Commit a validated snapshot through the existing XLSX import rules."""
    import asyncio
    from hashlib import sha256

    from services.business_time import get_business_timezone_name
    from services.star_rating_import import (
        import_star_rating_workbook,
        parse_star_rating_workbook,
    )
    from services.visit_import import (
        ImportIssue,
        VisitWorkbookError,
        create_import_batch,
        fail_import_batch,
        import_parsed_workbook,
        parse_visit_workbook,
    )

    content = workbook_bytes(kind, rows)
    import_type = "rating" if kind == "rating" else "detail"
    batch_id = await create_import_batch(
        conn,
        filename=f"source-{kind}-{start_date.isoformat()}.xlsx",
        file_sha256=sha256(content).hexdigest(),
        file_size=len(content),
        uploader_id=user_id,
        import_type=import_type,
        source_type=source_type,
        source_run_id=source_run_id,
    )
    try:
        async with conn.cursor() as cur:
            timezone_name = await get_business_timezone_name(cur)
        if import_type == "detail":
            parsed = await asyncio.to_thread(parse_visit_workbook, content, timezone_name)
            if any(issue.severity == "error" for issue in parsed.issues):
                raise VisitWorkbookError("来源快照存在无效走访记录，已阻止替换当前数据")
            result = await import_parsed_workbook(
                conn,
                batch_id=batch_id,
                parsed=parsed,
                replace_range=replace_range,
            )
        else:
            parsed = await asyncio.to_thread(parse_star_rating_workbook, content, timezone_name)
            if any(issue.severity == "error" for issue in parsed.issues):
                raise VisitWorkbookError("来源快照存在无效星级记录，已阻止替换当前数据")
            result = await import_star_rating_workbook(
                conn,
                batch_id=batch_id,
                parsed=parsed,
                replace_range=replace_range,
            )
        return result
    except VisitWorkbookError as exc:
        await fail_import_batch(
            conn,
            batch_id,
            str(exc),
            [ImportIssue("error", "invalid_source_snapshot", 0, str(exc), {})],
        )
        return {"batch_id": batch_id, "status": "failed", "message": str(exc)}
    except Exception:
        await fail_import_batch(conn, batch_id, "来源快照入库失败，当前数据未替换")
        raise
