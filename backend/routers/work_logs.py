"""工作日志草稿与 DOCX 导出。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal
from urllib.parse import quote

import aiomysql
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from database import get_db
from deps import require_admin
from services.audit import record_admin_audit, request_audit_fields
from services.work_log_data import build_system_snapshot
from services.work_log_document import build_daily_document
from services.work_log_schema import (
    TEMPLATE_VERSION,
    default_manual_values,
    field_definitions,
    get_schema,
    sanitize_values,
)


router = APIRouter(prefix="/api/work-logs", tags=["工作日志"])


class DraftCreate(BaseModel):
    report_type: Literal["daily"]
    business_date: date


class DraftSave(BaseModel):
    version: int = Field(ge=1)
    manual_values: dict[str, Any] = Field(default_factory=dict)
    override_values: dict[str, Any] = Field(default_factory=dict)


class VersionRequest(BaseModel):
    version: int = Field(ge=1)


def _json_load(value: Any, default):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _draft_payload(row, user: dict) -> dict:
    if not row:
        raise HTTPException(status_code=404, detail="日报草稿不存在")
    (
        draft_id,
        report_type,
        business_date,
        owner_user_id,
        owner_username,
        template_version,
        system_snapshot,
        manual_values,
        override_values,
        version,
        last_export_at,
        created_at,
        updated_at,
    ) = row
    return {
        "id": draft_id,
        "report_type": report_type,
        "business_date": _date_text(business_date),
        "owner": {
            "id": owner_user_id,
            "username": owner_username,
        },
        "can_edit": int(owner_user_id) == int(user["id"]),
        "template_version": template_version,
        "system_snapshot": _json_load(system_snapshot, {}),
        "manual_values": _json_load(manual_values, {}),
        "override_values": _json_load(override_values, {}),
        "version": version,
        "last_export_at": _date_text(last_export_at),
        "created_at": _date_text(created_at),
        "updated_at": _date_text(updated_at),
    }


async def _select_draft(cur, *, draft_id=None, report_type=None, business_date=None):
    columns = (
        "id, report_type, business_date, owner_user_id, owner_username, "
        "template_version, system_snapshot, manual_values, override_values, "
        "version, last_export_at, created_at, updated_at "
    )
    if draft_id is not None:
        await cur.execute(
            f"SELECT {columns} FROM _work_log_drafts WHERE id=%s",
            (draft_id,),
        )
    else:
        await cur.execute(
            f"SELECT {columns} FROM _work_log_drafts "
            "WHERE report_type=%s AND business_date=%s",
            (report_type, business_date),
        )
    return await cur.fetchone()


def _effective_values(draft: dict) -> dict:
    snapshot = draft.get("system_snapshot") or {}
    return {
        **(snapshot.get("values") or {}),
        **(draft.get("manual_values") or {}),
        **(draft.get("override_values") or {}),
    }


def _missing_items(draft: dict) -> list[dict]:
    values = _effective_values(draft)
    sources = (draft.get("system_snapshot") or {}).get("sources") or {}
    missing = []
    for field_id, definition in field_definitions().items():
        if not definition.get("required"):
            continue
        value = values.get(field_id)
        empty = value is None or value == "" or (
            definition["type"] == "table" and not value
        )
        if not empty:
            continue
        source_message = ""
        if definition["source"] == "system":
            source_key = (
                "model_three"
                if field_id.startswith("priority.model3_")
                else field_id.split(".", 1)[0]
            )
            source_message = (sources.get(source_key) or {}).get("message", "")
        missing.append({
            "field_id": field_id,
            "label": definition["label"],
            "section": field_id.split(".", 1)[0],
            "reason": source_message or "尚未填写",
        })
    return missing


@router.get("/schema")
async def schema(user: dict = Depends(require_admin)):
    del user
    return get_schema()


@router.post("/drafts")
async def create_draft(
    data: DraftCreate,
    request: Request,
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        row = await _select_draft(
            cur,
            report_type=data.report_type,
            business_date=data.business_date,
        )
    if row:
        return _draft_payload(row, user)

    snapshot = await build_system_snapshot(conn, data.business_date)
    manual_values = default_manual_values()
    created = False
    async with conn.cursor() as cur:
        try:
            await cur.execute(
                """
                INSERT INTO _work_log_drafts (
                    report_type, business_date, owner_user_id, owner_username,
                    template_version, system_snapshot, manual_values,
                    override_values, version, created_by, updated_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
                """,
                (
                    data.report_type,
                    data.business_date,
                    user["id"],
                    user["username"],
                    TEMPLATE_VERSION,
                    json.dumps(snapshot, ensure_ascii=False),
                    json.dumps(manual_values, ensure_ascii=False),
                    "{}",
                    user["id"],
                    user["id"],
                ),
            )
            draft_id = cur.lastrowid
            created = True
        except aiomysql.IntegrityError:
            draft_id = None
        row = await _select_draft(
            cur,
            draft_id=draft_id,
            report_type=data.report_type,
            business_date=data.business_date,
        )
    if created:
        await record_admin_audit(
            user,
            "work_log.create",
            target_type="work_log_draft",
            target_name=str(row[0]),
            detail={
                "report_type": data.report_type,
                "business_date": data.business_date.isoformat(),
            },
            **request_audit_fields(request),
        )
    return _draft_payload(row, user)


@router.get("/drafts/by-date/{report_type}/{business_date}")
async def get_draft(
    report_type: Literal["daily"],
    business_date: date,
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        row = await _select_draft(
            cur,
            report_type=report_type,
            business_date=business_date,
        )
    return _draft_payload(row, user)


@router.put("/drafts/{draft_id}")
async def save_draft(
    draft_id: int,
    data: DraftSave,
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    manual_values = sanitize_values(data.manual_values, source="manual")
    override_values = sanitize_values(data.override_values, source="system")
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE _work_log_drafts
            SET manual_values=%s, override_values=%s, version=version+1,
                updated_by=%s
            WHERE id=%s AND owner_user_id=%s AND version=%s
            """,
            (
                json.dumps(manual_values, ensure_ascii=False),
                json.dumps(override_values, ensure_ascii=False),
                user["id"],
                draft_id,
                user["id"],
                data.version,
            ),
        )
        if cur.rowcount != 1:
            row = await _select_draft(cur, draft_id=draft_id)
            if not row:
                raise HTTPException(status_code=404, detail="日报草稿不存在")
            if int(row[3]) != int(user["id"]):
                raise HTTPException(
                    status_code=403,
                    detail="当前草稿由其他管理员编辑，请先接管编辑权",
                )
            raise HTTPException(
                status_code=409,
                detail="草稿已在其他页面更新，请刷新后继续",
            )
        row = await _select_draft(cur, draft_id=draft_id)
    return _draft_payload(row, user)


@router.post("/drafts/{draft_id}/takeover")
async def takeover_draft(
    draft_id: int,
    request: Request,
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        before = await _select_draft(cur, draft_id=draft_id)
        if not before:
            raise HTTPException(status_code=404, detail="日报草稿不存在")
        await cur.execute(
            """
            UPDATE _work_log_drafts
            SET owner_user_id=%s, owner_username=%s, version=version+1,
                updated_by=%s
            WHERE id=%s
            """,
            (user["id"], user["username"], user["id"], draft_id),
        )
        row = await _select_draft(cur, draft_id=draft_id)
    await record_admin_audit(
        user,
        "work_log.takeover",
        target_type="work_log_draft",
        target_name=str(draft_id),
        detail={"previous_owner": before[4]},
        **request_audit_fields(request),
    )
    return _draft_payload(row, user)


@router.post("/drafts/{draft_id}/refresh")
async def refresh_draft(
    draft_id: int,
    data: VersionRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        row = await _select_draft(cur, draft_id=draft_id)
    if not row:
        raise HTTPException(status_code=404, detail="日报草稿不存在")
    if int(row[3]) != int(user["id"]):
        raise HTTPException(status_code=403, detail="只有当前编辑人可以刷新系统数据")
    snapshot = await build_system_snapshot(conn, row[2])
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE _work_log_drafts
            SET system_snapshot=%s, version=version+1, updated_by=%s
            WHERE id=%s AND owner_user_id=%s AND version=%s
            """,
            (
                json.dumps(snapshot, ensure_ascii=False),
                user["id"],
                draft_id,
                user["id"],
                data.version,
            ),
        )
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="草稿已在其他页面更新，请刷新后继续",
            )
        row = await _select_draft(cur, draft_id=draft_id)
    await record_admin_audit(
        user,
        "work_log.refresh",
        target_type="work_log_draft",
        target_name=str(draft_id),
        detail={"business_date": _date_text(row[2])},
        **request_audit_fields(request),
    )
    return _draft_payload(row, user)


@router.get("/drafts/{draft_id}/missing")
async def check_missing(
    draft_id: int,
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        row = await _select_draft(cur, draft_id=draft_id)
    draft = _draft_payload(row, user)
    missing = _missing_items(draft)
    return {"missing": missing, "count": len(missing)}


@router.post("/drafts/{draft_id}/export")
async def export_draft(
    draft_id: int,
    request: Request,
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        row = await _select_draft(cur, draft_id=draft_id)
    draft = _draft_payload(row, user)
    content, filename = build_daily_document(
        draft,
        get_schema(),
        _effective_values(draft),
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE _work_log_drafts SET last_export_at=UTC_TIMESTAMP() WHERE id=%s",
            (draft_id,),
        )
    await record_admin_audit(
        user,
        "work_log.export",
        target_type="work_log_draft",
        target_name=str(draft_id),
        detail={
            "business_date": draft["business_date"],
            "missing_count": len(_missing_items(draft)),
        },
        **request_audit_fields(request),
    )
    return Response(
        content=content,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": (
                "attachment; filename*=UTF-8''"
                + quote(filename)
            )
        },
    )
