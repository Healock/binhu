"""Shadow-only, versioned readback contract for derived consumers.

Events carry identifiers and metadata only. A derived consumer must present
the shadow environment identity and use this endpoint to read one
revision-fenced local task snapshot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from config import settings
from database import get_db
from services.parsers import TABLE_NAMES


router = APIRouter(
    prefix="/internal/v1/derived-input",
    tags=["派生消费者回读"],
)

ALLOWED_VALUE_FIELDS = frozenset({
    "address", "standard_address", "community", "small_community",
    "inspector", "task_state", "check_result", "task_type",
})

_LOCAL_TASK_ID_RE = re.compile(r"^(?P<table>t_[A-Za-z0-9_]+):(?P<id>[1-9][0-9]*)$")
_LOCAL_TABLE_NAMES = frozenset(str(name) for name in TABLE_NAMES.values() if name)
_LOCAL_TABLE_TO_PARSER = {
    str(table_name): str(parser_type)
    for parser_type, table_name in TABLE_NAMES.items()
    if table_name
}
_MAX_VALUE_LENGTH = 500
_MAX_TASK_ID_LENGTH = 128
_MAX_LOCAL_TASK_ID = 2**63 - 1
_SOURCE_REVISION_MISMATCH = {
    "code": "source_revision_mismatch",
    "message": "本地来源与 canonical 来源记录版本不一致，暂不可回读",
}


def _json_object(value: Any, *, label: str = "来源") -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(500, detail={
                "code": "derived_input_invalid_value",
                "message": f"{label}内容无法解析",
            }) from exc
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(500, detail={
                "code": "derived_input_invalid_value",
                "message": f"{label}内容无法解析",
            }) from exc
    if not isinstance(value, dict):
        raise HTTPException(500, detail={
            "code": "derived_input_invalid_value",
            "message": f"{label}内容不是对象",
        })
    return value


def _safe_scalar(value: Any, field: str) -> str:
    """Convert one selected value without allowing nested JSON through."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)) or not isinstance(
        value, (str, int, float, bool)
    ):
        raise HTTPException(500, detail={
            "code": "derived_input_invalid_value",
            "field": field,
            "message": "派生字段必须是标量",
        })
    if isinstance(value, float) and not math.isfinite(value):
        raise HTTPException(500, detail={
            "code": "derived_input_invalid_value",
            "field": field,
            "message": "派生字段数值无效",
        })
    text = str(value)
    if len(text) > _MAX_VALUE_LENGTH:
        raise HTTPException(500, detail={
            "code": "derived_input_invalid_value",
            "field": field,
            "message": "派生字段长度超出限制",
        })
    return text


def _hash_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utc_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(500, detail={
                "code": "derived_input_invalid_value",
                "message": "更新时间格式无效",
            }) from exc
        aware = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        return aware.isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return aware.isoformat().replace("+00:00", "Z")
    raise HTTPException(500, detail={
        "code": "derived_input_invalid_value",
        "message": "更新时间类型无效",
    })


def _check_internal_token(token: str | None) -> None:
    expected = str(settings.DERIVED_READBACK_TOKEN or "")
    if not expected:
        raise HTTPException(status_code=503, detail={
            "code": "derived_readback_disabled",
            "message": "派生消费者回读接口尚未配置内部凭据",
        })
    if not token or not hmac.compare_digest(str(token), expected):
        raise HTTPException(status_code=401, detail={
            "code": "invalid_internal_credential",
            "message": "内部消费者凭据无效",
        })


def _check_shadow_context(token: str | None, environment: str | None, run_id: str | None) -> None:
    if settings.APP_ENVIRONMENT != "shadow" or not str(settings.LOAD_TEST_RUN_ID or "").strip():
        raise HTTPException(status_code=503, detail={
            "code": "derived_readback_disabled",
            "message": "派生消费者回读接口仅在配置完整的影子环境开放",
        })
    _check_internal_token(token)
    expected_run_id = str(settings.LOAD_TEST_RUN_ID).strip()
    if environment != settings.APP_ENVIRONMENT or run_id != expected_run_id:
        raise HTTPException(status_code=403, detail={
            "code": "shadow_context_mismatch",
            "message": "请求环境或影子运行编号与当前服务不一致",
        })


async def _require_shadow_context(
    x_binhu_internal_token: str | None = Header(default=None, alias="X-Binhu-Internal-Token"),
    x_binhu_environment: str | None = Header(default=None, alias="X-Binhu-Environment"),
    x_binhu_run_id: str | None = Header(default=None, alias="X-Binhu-Run-Id"),
) -> None:
    """Authenticate before FastAPI resolves the database dependency."""
    _check_shadow_context(x_binhu_internal_token, x_binhu_environment, x_binhu_run_id)


def _requested_fields(fields: str) -> set[str]:
    requested = {item.strip() for item in fields.split(",") if item.strip()}
    unknown = requested - ALLOWED_VALUE_FIELDS
    if unknown:
        raise HTTPException(status_code=422, detail={
            "code": "unsupported_readback_field",
            "fields": sorted(unknown),
        })
    if not requested:
        requested = {"community", "small_community", "task_state", "task_type"}
    return requested


def _parse_local_task_id(task_id: str) -> tuple[str, int]:
    text = str(task_id or "").strip()
    if len(text) > _MAX_TASK_ID_LENGTH:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_local_task_id",
            "message": "task_id 必须使用本地表名:id格式",
        })
    match = _LOCAL_TASK_ID_RE.fullmatch(text)
    identifier = match.group("id") if match else ""
    if not match or match.group("table") not in _LOCAL_TABLE_NAMES:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_local_task_id",
            "message": "task_id 必须使用本地表名:id格式",
        })
    if len(identifier) > 19:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_local_task_id",
            "message": "task_id 必须使用本地表名:id格式",
        })
    local_task_id = int(identifier)
    if local_task_id > _MAX_LOCAL_TASK_ID:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_local_task_id",
            "message": "task_id 必须使用本地表名:id格式",
        })
    return match.group("table"), local_task_id


def _first_value(values: dict[str, Any], fields: tuple[str, ...]) -> Any:
    for field in fields:
        if field in values and values[field] not in (None, ""):
            return values[field]
    return ""


def _stable_field_values(
    parser_type: str,
    requested: set[str],
    source_values: dict[str, Any],
    projection_values: dict[str, Any],
    projection_community: Any,
    projection_small_community: Any,
    projection_inspector: Any,
    projection_task_state: Any,
    projection_address: Any,
) -> dict[str, str]:
    """Map parser-owned Chinese columns to the stable readback contract."""
    try:
        from services.task_workflow import TASK_WORKFLOWS
        workflow = TASK_WORKFLOWS.get(parser_type)
    except Exception:
        workflow = None
    address_fields = tuple(getattr(workflow, "address_fields", ())) or (
        "地址", "房屋地址", "地址1", "现住址", "疑似现住址", "出租屋地址",
    )
    result_field = str(getattr(workflow, "result_field", "核查结果"))
    community = projection_community or _first_value(
        source_values, ("社区", "下发社区", "community")
    )
    inspector = projection_inspector or _first_value(
        source_values, ("核查人", "inspector")
    )
    task_state = projection_task_state or projection_values.get("task_state", "")
    standard_address = _first_value(
        projection_values,
        ("standard_address", "标准地址"),
    ) or projection_address or ""
    values: dict[str, Any] = {
        "address": _first_value(source_values, address_fields + ("address",)),
        "standard_address": standard_address,
        "community": community,
        "small_community": projection_small_community or _first_value(
            projection_values, ("small_community", "小区", "小区名称")
        ),
        "inspector": inspector,
        "task_state": task_state,
        "check_result": _first_value(source_values, (result_field, "核查结果", "核查反馈", "check_result")),
        "task_type": parser_type,
    }
    return {field: _safe_scalar(values[field], field) for field in requested}


async def _read_derived_input(
    task_id: str,
    *,
    source_id: int | None,
    revision: int | None,
    fields: str,
    conn,
) -> dict[str, Any]:
    requested = _requested_fields(fields)
    table_name, local_task_id = _parse_local_task_id(task_id)
    expected_parser_type = _LOCAL_TABLE_TO_PARSER.get(table_name)
    if source_id is None or revision is None:
        raise HTTPException(status_code=422, detail={
            "code": "source_identity_required",
            "message": "source_id 和 revision 必须同时提供",
        })
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT source.id, source.parser_type, source.row_key,
                   source.revision, source.row_hash, source.values_json,
                   projection.values_json, projection.community,
                   projection.small_community_name, projection.inspector,
                   projection.task_state, projection.source_revision,
                   projection.assignment_address_display, projection.updated_at,
                   local_source.revision, local_source.content_hash,
                   source.source_kind, source.source_ref
            FROM _online_source_rows AS source
            JOIN _local_source_records AS local_source
              ON local_source.source_kind IN ('local_table','local_dispatch')
             AND local_source.source_ref=source.source_ref
             AND local_source.parser_type=source.parser_type
             AND local_source.local_task_id=source.physical_row
             AND local_source.status='active'
             AND local_source.archived_at IS NULL
            LEFT JOIN _online_source_projection AS projection
              ON projection.parser_type=source.parser_type
             AND projection.row_key=source.row_key
            WHERE source.id=%s
              AND source.source_kind IN ('local_table','local_dispatch')
              AND source.spreadsheet_id=0
              AND source.physical_row=%s
              AND source.archived_at IS NULL
              AND source.parser_type=%s
              AND NOT EXISTS (
                  SELECT 1
                  FROM _local_source_records AS duplicate
                  WHERE duplicate.parser_type=source.parser_type
                    AND duplicate.business_key=source.row_key
                    AND duplicate.source_kind IN ('local_table','local_dispatch')
                    AND duplicate.status='active'
                    AND duplicate.archived_at IS NULL
                    AND duplicate.id<>local_source.id
                  LIMIT 1
              )
            LIMIT 1
            """,
            (
                int(source_id), local_task_id, expected_parser_type,
            ),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={
            "code": "derived_input_not_found", "task_id": task_id,
        })
    if str(row[1] or "") != expected_parser_type:
        raise HTTPException(status_code=404, detail={
            "code": "derived_input_not_found", "task_id": task_id,
        })

    source_kind = str(row[16] or "")
    if source_kind != "local_table":
        raise HTTPException(status_code=422, detail={
            "code": "unsupported_source_kind",
            "message": "当前回读接口仅支持 local_table 来源",
        })
    if str(row[17] or "") != task_id:
        raise HTTPException(status_code=404, detail={
            "code": "derived_input_not_found", "task_id": task_id,
        })

    current_revision = int(row[3] or 0)
    if revision != current_revision:
        raise HTTPException(status_code=409, detail={
            "code": "revision_changed",
            "task_id": task_id,
            "current_revision": current_revision,
        })
    local_revision = None if row[14] is None else int(row[14])
    projection_revision = None if row[11] is None else int(row[11])
    if local_revision != current_revision:
        raise HTTPException(status_code=409, detail={
            **_SOURCE_REVISION_MISMATCH,
            "task_id": task_id,
            "source_revision": current_revision,
            "local_revision": local_revision,
        })
    if projection_revision != current_revision:
        raise HTTPException(status_code=409, detail={
            "code": "projection_revision_mismatch",
            "message": "本地任务投影尚未追上来源版本，请稍后重试",
            "task_id": task_id,
            "source_revision": current_revision,
            "projection_revision": projection_revision,
        })
    if str(row[4] or "") != str(row[15] or ""):
        raise HTTPException(status_code=409, detail={
            "code": "source_content_mismatch",
            "message": "本地来源内容哈希不一致，请稍后重试",
            "task_id": task_id,
        })

    source_values = _json_object(row[5])
    projection_values = _json_object(row[6], label="投影")
    selected = _stable_field_values(
        str(row[1]), requested, source_values, projection_values,
        row[7], row[8], row[9], row[10], row[12],
    )
    envelope = {
        "task_id": task_id,
        "source_id": int(row[0]),
        "parser_type": str(row[1]),
        "row_key": str(row[2]),
        "revision": current_revision,
        "content_hash": str(row[4]),
        "fields": selected,
        "updated_at": _utc_iso(row[13]),
        "environment": settings.APP_ENVIRONMENT,
        "run_id": str(settings.LOAD_TEST_RUN_ID or "").strip(),
    }
    envelope["readback_hash"] = _hash_payload(envelope)
    return envelope


@router.get("/tasks/{task_id}", dependencies=[Depends(_require_shadow_context)])
async def read_derived_input_route(
    task_id: str,
    source_id: int = Query(..., ge=1),
    revision: int = Query(..., ge=0),
    fields: str = Query(default="", max_length=500),
    conn=Depends(get_db),
):
    return await _read_derived_input(
        task_id, source_id=source_id, revision=revision, fields=fields, conn=conn,
    )
