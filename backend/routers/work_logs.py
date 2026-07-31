"""工作日志草稿与 PDF 导出。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal
from urllib.parse import quote

import aiomysql
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from database import get_db
from deps import require_permission
from services.permissions import WORK_LOG_MANAGE
from services.audit import record_admin_audit, request_audit_fields
from services.work_log_data import build_system_snapshot
from services.work_log_pdf import build_daily_pdf
from services.work_log_schema import (
    TEMPLATE_VERSION,
    default_manual_values,
    effective_values,
    field_definitions,
    fill_community_grid_member_counts,
    get_schema,
    leaf_columns,
    sanitize_values,
)


router = APIRouter(prefix="/api/work-logs", tags=["工作日志"])
require_admin = require_permission(WORK_LOG_MANAGE)


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


def _draft_summary(row) -> dict:
    (
        draft_id,
        report_type,
        business_date,
        owner_user_id,
        owner_username,
        template_version,
        version,
        last_export_at,
        created_at,
        updated_at,
        created_by,
        creator_username,
    ) = row
    return {
        "id": draft_id,
        "report_type": report_type,
        "business_date": _date_text(business_date),
        "owner": {
            "id": owner_user_id,
            "username": owner_username,
        },
        "creator": {
            "id": created_by,
            "username": creator_username or f"用户#{created_by}",
        },
        "template_version": template_version,
        "version": version,
        "last_export_at": _date_text(last_export_at),
        "created_at": _date_text(created_at),
        "updated_at": _date_text(updated_at),
    }


LEGACY_FIELD_MAP = {
    "basic.total_population": "flow.population.total",
    "basic.registered_population": "flow.population.registered",
    "basic.floating_population": "flow.population.floating",
    "basic.flow_added": "flow.registration.added",
    "basic.active_cancelled": "flow.registration.active_cancelled",
    "basic.passive_cancelled": "flow.registration.passive_cancelled",
    "rental.current_stock": "rental.stock.rented",
    "rental.reverse_checks": "rental.reverse.houses",
    "rental.analysis": "rental.reverse_analysis",
    "self_owned.analysis": "self_owned.analysis",
    "priority.added": "priority.management.added",
    "priority.removed": "priority.management.removed",
    "disputes.stock": "disputes.stock.total",
    "disputes.added": "disputes.daily.added",
    "disputes.resolved": "disputes.daily.archive_resolved",
    "fire.checked": "fire.daily.checked",
    "fire.hazards": "fire.daily.hazards",
    "fire.rectified": "fire.daily.rectified",
    "security.venues_checked": "security.venues.checked",
    "security.dogs": "security.dog.penalties",
    "security.special_cases": "security.yellow_gamble.yellow_cases",
    "security.analysis": "security.yellow_gamble_analysis",
    "fraud.cases": "fraud.cases.daily",
    "fraud.warnings": "fraud.warning.total",
    "fraud.completed": "fraud.warning.met",
    "fraud.analysis": "fraud.large_amount_case",
}


async def _upgrade_legacy_draft(conn, row, user: dict):
    """把 daily-v1 草稿升级为 v2，并在快照中完整保留旧 JSON。"""
    if not row or row[5] != "daily-v1":
        return row
    old_snapshot = _json_load(row[6], {})
    old_manual = _json_load(row[7], {})
    old_overrides = _json_load(row[8], {})
    new_snapshot = await build_system_snapshot(conn, row[2])
    new_snapshot["legacy_v1"] = {
        "template_version": row[5],
        "system_snapshot": old_snapshot,
        "manual_values": old_manual,
        "override_values": old_overrides,
    }
    manual_values = default_manual_values(
        new_snapshot.get("communities") or [],
        new_snapshot.get("community_officers") or {},
        new_snapshot.get("community_grid_member_counts") or {},
    )
    for old_id, new_id in LEGACY_FIELD_MAP.items():
        value = (
            old_overrides.get(old_id)
            if old_id in old_overrides
            else old_manual.get(old_id)
        )
        if value not in (None, "", []):
            manual_values[new_id] = value
    manual_values = sanitize_values(manual_values, source="manual")
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE _work_log_drafts
            SET template_version=%s, system_snapshot=%s, manual_values=%s,
                override_values=%s, version=version+1, updated_by=%s
            WHERE id=%s AND template_version<>%s
            """,
            (
                TEMPLATE_VERSION,
                json.dumps(new_snapshot, ensure_ascii=False),
                json.dumps(manual_values, ensure_ascii=False),
                "{}",
                user["id"],
                row[0],
                TEMPLATE_VERSION,
            ),
        )
        return await _select_draft(cur, draft_id=row[0])


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
    return effective_values(draft)


def _missing_items(draft: dict) -> list[dict]:
    values = _effective_values(draft)
    sources = (draft.get("system_snapshot") or {}).get("sources") or {}
    missing = []
    for field_id, definition in field_definitions().items():
        if not definition.get("required"):
            continue
        value = values.get(field_id)
        empty = value is None or value == ""
        if definition["type"] == "table":
            rows = value if isinstance(value, list) else []
            required_columns = [
                item
                for item in leaf_columns(definition["columns"])
                if item.get("required", True)
            ]
            empty = not rows or any(
                any(row.get(item["key"]) in (None, "") for item in required_columns)
                for row in rows
                if isinstance(row, dict)
            )
        if not empty:
            continue
        source_message = ""
        if definition["source"] in {"system", "derived"}:
            source_key = definition.get("source_key", "")
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


@router.get("/drafts")
async def list_drafts(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    del user
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

    clauses: list[str] = []
    params: list[Any] = []
    if start_date:
        clauses.append("d.business_date >= %s")
        params.append(start_date)
    if end_date:
        clauses.append("d.business_date <= %s")
        params.append(end_date)
    normalized_keyword = (keyword or "").strip()
    if normalized_keyword:
        clauses.append(
            "(d.owner_username LIKE %s OR COALESCE(creator.username, '') LIKE %s)"
        )
        pattern = f"%{normalized_keyword}%"
        params.extend([pattern, pattern])
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    from_clause = """
        FROM _work_log_drafts d
        LEFT JOIN _users creator ON creator.id = d.created_by
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT COUNT(*) {from_clause} {where_clause}",
            tuple(params),
        )
        total_row = await cur.fetchone()
        total = int(total_row[0] or 0)
        await cur.execute(
            f"""
            SELECT
                d.id, d.report_type, d.business_date,
                d.owner_user_id, d.owner_username, d.template_version,
                d.version, d.last_export_at, d.created_at, d.updated_at,
                d.created_by, creator.username
            {from_clause}
            {where_clause}
            ORDER BY d.business_date DESC, d.updated_at DESC, d.id DESC
            LIMIT %s OFFSET %s
            """,
            (*params, page_size, (page - 1) * page_size),
        )
        rows = await cur.fetchall()
    return {
        "data": [_draft_summary(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


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
        row = await _upgrade_legacy_draft(conn, row, user)
        return _draft_payload(row, user)

    snapshot = await build_system_snapshot(conn, data.business_date)
    manual_values = default_manual_values(
        snapshot.get("communities") or [],
        snapshot.get("community_officers") or {},
        snapshot.get("community_grid_member_counts") or {},
    )
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
    row = await _upgrade_legacy_draft(conn, row, user)
    return _draft_payload(row, user)


@router.delete("/drafts/{draft_id}")
async def delete_draft(
    draft_id: int,
    request: Request,
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        row = await _select_draft(cur, draft_id=draft_id)
        if not row:
            raise HTTPException(status_code=404, detail="日报草稿不存在")
        await cur.execute(
            "DELETE FROM _work_log_drafts WHERE id=%s",
            (draft_id,),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="草稿状态已变化，请刷新后重试")
    await record_admin_audit(
        user,
        "work_log.delete",
        target_type="work_log_draft",
        target_name=str(draft_id),
        detail={
            "report_type": row[1],
            "business_date": _date_text(row[2]),
            "previous_owner": row[4],
            "version": row[9],
        },
        **request_audit_fields(request),
    )
    return {"message": "草稿已删除", "id": draft_id}


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
    before = await _upgrade_legacy_draft(conn, before, user)
    async with conn.cursor() as cur:
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
    row = await _upgrade_legacy_draft(conn, row, user)
    if not row:
        raise HTTPException(status_code=404, detail="日报草稿不存在")
    if int(row[3]) != int(user["id"]):
        raise HTTPException(status_code=403, detail="只有当前编辑人可以刷新系统数据")
    snapshot = await build_system_snapshot(conn, row[2])
    manual_values = fill_community_grid_member_counts(
        _json_load(row[7], {}),
        snapshot.get("community_grid_member_counts") or {},
    )
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE _work_log_drafts
            SET system_snapshot=%s, manual_values=%s,
                version=version+1, updated_by=%s
            WHERE id=%s AND owner_user_id=%s AND version=%s
            """,
            (
                json.dumps(snapshot, ensure_ascii=False),
                json.dumps(manual_values, ensure_ascii=False),
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
    row = await _upgrade_legacy_draft(conn, row, user)
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
    row = await _upgrade_legacy_draft(conn, row, user)
    draft = _draft_payload(row, user)
    content, filename = build_daily_pdf(
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
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                "attachment; filename*=UTF-8''"
                + quote(filename)
            )
        },
    )
