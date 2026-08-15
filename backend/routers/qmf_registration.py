"""全民防模型三单条只读预演 API。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from database import get_db
from deps import require_permission
from routers.mobile_tasks import _mobile_task_detail_data
from services.audit import record_admin_audit, request_audit_fields
from services.online_source import source_row_hash
from services.permissions import ONLINE_RAW_VIEW
from services.qmf_registration import (
    ALLOWED_PLATFORM_USERNAME,
    MODEL_THREE_PARSER,
    QmfPreviewError,
    SUPPORTED_RESULT,
    normalize_identity,
    preview_configured,
    run_guarded_preview,
    valid_identity,
)


router = APIRouter(prefix="/api/qmf-registration", tags=["全民防只读预演"])


class QmfPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parser_type: str = Field(min_length=1, max_length=100)
    row_key: str = Field(min_length=1, max_length=190)
    source_id: int = Field(gt=0)
    expected_revision: int = Field(ge=0)


def _safe_error_detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


async def _record_preview_audit(
    *,
    request: Request,
    user: dict,
    source_id: int,
    result: str,
    started_at: float,
    error_code: str = "",
    photo: dict[str, Any] | None = None,
) -> None:
    detail: dict[str, Any] = {
        "source_id": int(source_id),
        "mode": "read_only",
        "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
        "result_code": error_code or "success",
    }
    if photo:
        detail["photo"] = {
            "mime_type": str(photo.get("mime_type") or "")[:50],
            "size_bytes": int(photo.get("size_bytes") or 0),
            "sha256": str(photo.get("sha256") or "")[:64],
        }
    await record_admin_audit(
        user,
        "qmf_registration.preview",
        target_type="online_source_row",
        target_name=str(source_id),
        result=result,
        detail=detail,
        **request_audit_fields(request),
    )


async def _assert_source_unchanged(
    conn,
    *,
    parser_type: str,
    row_key: str,
    source_id: int,
    expected_revision: int,
    expected_hash: str,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT source.revision, source.row_hash,
                   projection.source_count, projection.conflict
            FROM _online_source_rows AS source
            JOIN _online_source_projection AS projection
              ON projection.parser_type=source.parser_type
             AND projection.row_key=source.row_key
            WHERE source.id=%s AND source.parser_type=%s AND source.row_key=%s
            """,
            (source_id, parser_type, row_key),
        )
        row = await cur.fetchone()
    if not row:
        raise QmfPreviewError("source_missing", "腾讯来源行已不存在，请刷新后重试", 409)
    if int(row[0]) != expected_revision or str(row[1]) != expected_hash:
        raise QmfPreviewError("source_changed", "腾讯来源行已变化，请刷新后重试", 409)
    if int(row[2] or 0) != 1 or bool(row[3]):
        raise QmfPreviewError("source_not_unique", "任务来源状态已变化，请刷新后重试", 409)


@router.post("/preview")
async def preview_qmf_registration(
    data: QmfPreviewRequest,
    request: Request,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    started_at = time.monotonic()
    if user.get("username") != ALLOWED_PLATFORM_USERNAME:
        raise HTTPException(403, "当前账号不能使用全民防只读预演")
    try:
        if not preview_configured():
            raise QmfPreviewError(
                "preview_not_configured",
                "全民防只读预演尚未完成安全配置",
                503,
            )
        if data.parser_type != MODEL_THREE_PARSER:
            raise QmfPreviewError("parser_not_supported", "第一版仅支持疑似未注销模型三", 422)
        detail = await _mobile_task_detail_data(
            data.parser_type,
            data.row_key,
            user,
            conn,
            include_photo_requests=False,
        )
        sources = detail.get("sources") or []
        if detail.get("task", {}).get("conflict") or len(sources) != 1:
            raise QmfPreviewError("source_not_unique", "该任务不是唯一有效来源", 409)
        source = sources[0]
        if int(source.get("id") or 0) != data.source_id:
            raise QmfPreviewError("source_mismatch", "腾讯来源行已变化，请刷新后重试", 409)
        if int(source.get("revision") or 0) != data.expected_revision:
            raise QmfPreviewError("source_revision_conflict", "腾讯来源版本已变化，请刷新后重试", 409)
        values = source.get("values") or {}
        current_hash = source_row_hash(values)
        if current_hash != str(source.get("row_hash") or ""):
            raise QmfPreviewError("source_hash_conflict", "腾讯来源摘要校验失败", 409)
        if str(values.get("核查结果") or "").strip() != SUPPORTED_RESULT:
            raise QmfPreviewError("result_not_supported", "第一版仅支持核查结果为“在吴”的任务", 422)
        identity = normalize_identity(values.get("身份证号"))
        if not valid_identity(identity):
            raise QmfPreviewError("identity_invalid", "任务身份证号格式无效", 422)
        name = str(values.get("姓名") or "").strip()
        if not name:
            raise QmfPreviewError("name_missing", "任务姓名为空，不能预演", 422)

        await _assert_source_unchanged(
            conn,
            parser_type=data.parser_type,
            row_key=data.row_key,
            source_id=data.source_id,
            expected_revision=data.expected_revision,
            expected_hash=current_hash,
        )
        platform_task = {
            "parser_type": data.parser_type,
            "row_key": data.row_key,
            "source_id": data.source_id,
            "name": name,
            "identity_number": identity,
            "phone": str(values.get("联系方式") or "").strip(),
            "address": str(values.get("地址") or "").strip(),
            "community": str(values.get("下发社区") or "").strip(),
            "result": SUPPORTED_RESULT,
        }
        result = await run_guarded_preview(platform_task=platform_task)
        # 上游读取可能持续数秒；返回敏感资料前再次确认平台来源仍未变化。
        await _assert_source_unchanged(
            conn,
            parser_type=data.parser_type,
            row_key=data.row_key,
            source_id=data.source_id,
            expected_revision=data.expected_revision,
            expected_hash=current_hash,
        )
        await _record_preview_audit(
            request=request,
            user=user,
            source_id=data.source_id,
            result="success",
            started_at=started_at,
            photo=result.get("photo"),
        )
        return JSONResponse(
            content=result,
            headers={
                "Cache-Control": "no-store, private",
                "Pragma": "no-cache",
            },
        )
    except QmfPreviewError as exc:
        await _record_preview_audit(
            request=request,
            user=user,
            source_id=data.source_id,
            result="failed",
            started_at=started_at,
            error_code=exc.code,
        )
        raise HTTPException(
            exc.status_code,
            _safe_error_detail(exc.code, exc.message),
        ) from exc
    except HTTPException:
        await _record_preview_audit(
            request=request,
            user=user,
            source_id=data.source_id,
            result="failed",
            started_at=started_at,
            error_code="platform_task_unavailable",
        )
        raise
    except Exception as exc:
        await _record_preview_audit(
            request=request,
            user=user,
            source_id=data.source_id,
            result="failed",
            started_at=started_at,
            error_code="unexpected_error",
        )
        raise HTTPException(
            500,
            _safe_error_detail(
                "unexpected_error", "全民防只读预演暂时不可用，请稍后重试"
            ),
        ) from exc
