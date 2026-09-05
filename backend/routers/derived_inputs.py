"""Versioned, allow-listed readback contract for derived consumers.

Events intentionally contain metadata only.  Flink and other consumers must
use this endpoint instead of opening their own MySQL connections.  The
endpoint is disabled until a dedicated internal token is configured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from config import settings
from database import get_db


router = APIRouter(prefix="/internal/v1/derived-input", tags=["派生消费者回读"])

# Only fields required by the first derived computations may be exposed.  The
# event itself never contains these values; this list is the single contract
# consumers share.
ALLOWED_VALUE_FIELDS = frozenset({
    "address", "standard_address", "community", "small_community",
    "inspector", "task_state", "check_result", "task_type",
})


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hash_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


@router.get("/tasks/{task_id}")
async def read_derived_input(
    task_id: str,
    x_binhu_internal_token: str | None = Header(default=None),
    source_id: int | None = Query(default=None, ge=1),
    revision: int | None = Query(default=None, ge=0),
    fields: str = Query(default="", max_length=500),
    conn=Depends(get_db),
):
    """Return the minimum versioned input required by a derived consumer."""
    _check_internal_token(x_binhu_internal_token)
    requested = {item.strip() for item in fields.split(",") if item.strip()}
    unknown = requested - ALLOWED_VALUE_FIELDS
    if unknown:
        raise HTTPException(status_code=422, detail={
            "code": "unsupported_readback_field",
            "fields": sorted(unknown),
        })
    if not requested:
        requested = {"community", "small_community", "task_state", "task_type"}

    async with conn.cursor() as cur:
        if source_id is not None:
            await cur.execute(
                """
                SELECT source.id, source.parser_type, source.row_key,
                       source.revision, source.row_hash, source.values_json,
                       projection.values_json, projection.community,
                       projection.task_state, projection.updated_at
                FROM _online_source_rows AS source
                LEFT JOIN _online_source_projection AS projection
                  ON projection.parser_type=source.parser_type
                 AND projection.row_key=source.row_key
                WHERE source.id=%s AND source.source_ref=%s
                LIMIT 1
                """,
                (source_id, task_id),
            )
        else:
            await cur.execute(
                """
                SELECT source.id, source.parser_type, source.row_key,
                       source.revision, source.row_hash, source.values_json,
                       projection.values_json, projection.community,
                       projection.task_state, projection.updated_at
                FROM _online_source_rows AS source
                LEFT JOIN _online_source_projection AS projection
                  ON projection.parser_type=source.parser_type
                 AND projection.row_key=source.row_key
                WHERE source.source_ref=%s
                ORDER BY source.id DESC LIMIT 1
                """,
                (task_id,),
            )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={
            "code": "derived_input_not_found", "task_id": task_id,
        })

    current_revision = int(row[3] or 0)
    if revision is not None and revision != current_revision:
        raise HTTPException(status_code=409, detail={
            "code": "revision_changed",
            "task_id": task_id,
            "current_revision": current_revision,
        })
    source_values = _json_object(row[5])
    projection_values = _json_object(row[6])
    combined = {**source_values, **projection_values}
    selected = {field: combined.get(field) for field in requested if field in combined}
    selected.setdefault("community", row[7] or "")
    selected.setdefault("task_state", row[8] or "")
    envelope = {
        "task_id": task_id,
        "source_id": int(row[0]),
        "parser_type": str(row[1]),
        "row_key": str(row[2]),
        "revision": current_revision,
        "content_hash": str(row[4]),
        "fields": selected,
        "updated_at": row[9].isoformat() if row[9] else None,
    }
    envelope["readback_hash"] = _hash_payload(envelope)
    return envelope
