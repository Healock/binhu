"""小区地址库、全链条批次预处理、审核、反馈导出与腾讯发布。"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from database import get_db
from deps import require_permission
from routers.query import (
    _enabled_spreadsheets,
    _load_source_row,
    _oauth_client,
    _refresh_spreadsheet,
    _row_values_match,
    _writeback_enabled,
)
from services.audit import record_admin_audit, request_audit_fields
from services.business_time import get_business_date
from services.online_source import acquire_sheet_lock, json_value, release_sheet_lock
from services.online_source import source_row_hash
from services.parsers import get_parser
from services.permissions import POLICE_ADDRESS_MANAGE, POLICE_DISPATCH_MANAGE
from services.police_dispatch import (
    FINAL_ACTIONS,
    MAX_POLICE_FILE_BYTES,
    PoliceWorkbookError,
    apply_preprocessing_suggestions,
    build_feedback_workbook,
    build_publish_address,
    normalize_lookup,
    parse_dispatch_workbook,
    publish_business_key,
    resolve_community,
    community_resolver,
    dispatch_field_roles,
    dispatch_values_from_raw,
    identity_digest,
    stable_json,
)
from services.txdocs_client import TxDocsAPIError


router = APIRouter(prefix="/api/police-dispatch", tags=["全链条下发"])


class AddressCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    detail_address: str = Field(default="", max_length=1000)
    address_type: Literal["community", "apartment", "other"] = "community"
    pattern: str = Field(default="", max_length=200)
    community_id: int
    aliases: list[str] = Field(default_factory=list, max_length=50)
    enabled: bool = True


class TaskReview(BaseModel):
    expected_version: int = Field(gt=0)
    final_action: Literal[
        "dispatch", "no_registration", "transfer", "duplicate_exclude"
    ]
    final_community_id: int | None = None
    review_note: str = Field(default="", max_length=1000)


class BulkReview(BaseModel):
    tasks: list[dict[str, int]] = Field(min_length=1, max_length=2000)
    mode: Literal["accept_suggestion", "set_action"] = "accept_suggestion"
    final_action: Literal[
        "dispatch", "no_registration", "transfer", "duplicate_exclude"
    ] | None = None
    final_community_id: int | None = None
    review_note: str = Field(default="", max_length=1000)


class TaskSearch(BaseModel):
    batch_id: int = Field(gt=0)
    status: str = Field(default="all", max_length=30)
    category: str = Field(default="all", max_length=30)
    keyword: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=500)


class TaskBusinessFieldsUpdate(BaseModel):
    expected_version: int = Field(gt=0)
    fields: dict[str, str] = Field(default_factory=dict, max_length=200)


class ConflictResolution(BaseModel):
    expected_version: int = Field(gt=0)
    strategy: Literal["adopt_tencent", "overwrite_tencent"]
    expected_row_hash: str = Field(min_length=64, max_length=64)
    confirmation: str = Field(default="", max_length=50)


ALLOWED_POLICE_POSITIONS = {"基础管控", "中队长"}


def _permission_group_codes(user: dict) -> set[str]:
    codes = {
        str(group.get("code") or "")
        for group in user.get("permission_groups") or []
        if group.get("code")
    }
    primary = (user.get("permission_group") or {}).get("code")
    if primary:
        codes.add(str(primary))
    return codes


def require_police_access(permission: str) -> Callable:
    base_dependency = require_permission(permission)

    async def dependency(user: dict = Depends(base_dependency)) -> dict:
        permission_scope = (user.get("permission_scopes") or {}).get(
            permission,
            user.get("data_scope"),
        )
        if permission_scope != "all":
            raise HTTPException(
                403,
                "数据预处理必须使用全所数据范围，请联系超级管理员修正权限组",
            )
        group_codes = _permission_group_codes(user)
        member = user.get("member")
        if member:
            if str(member.get("position") or "") in ALLOWED_POLICE_POSITIONS:
                return user
            raise HTTPException(403, "当前人员岗位不能进入数据预处理工作台")
        if group_codes.intersection({"admin", "super_admin"}) or (
            not group_codes and user.get("role") in {"admin", "super_admin"}
        ):
            return user
        raise HTTPException(403, "数据预处理仅向内勤和系统管理员开放")

    dependency.__name__ = f"require_police_{permission.replace('.', '_')}"
    return dependency


require_police_dispatch = require_police_access(POLICE_DISPATCH_MANAGE)
require_police_address = require_police_access(POLICE_ADDRESS_MANAGE)


def _safe_filename(filename: str | None, fallback: str) -> str:
    return Path((filename or fallback).replace("\\", "/")).name[:255]


async def _read_upload(file: UploadFile, *, allow_xls: bool = True) -> tuple[str, bytes]:
    filename = _safe_filename(file.filename, "下发数据.xlsx")
    suffixes = {".xlsx", ".xls"} if allow_xls else {".xlsx"}
    if Path(filename).suffix.lower() not in suffixes:
        raise HTTPException(400, "只支持 .xls 或 .xlsx 文件")
    content = await file.read(MAX_POLICE_FILE_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(400, "上传文件为空")
    if len(content) > MAX_POLICE_FILE_BYTES:
        raise HTTPException(413, "Excel 文件不能超过 30MB")
    return filename, content


async def _communities(cur) -> list[dict[str, Any]]:
    await cur.execute("""
        SELECT community.id, community.name,
               MAX(CASE WHEN community.is_active=1
                              AND department.is_active=1
                        THEN 1 ELSE 0 END) AS enabled
        FROM _communities AS community
        LEFT JOIN _departments AS department
          ON department.community_id=community.id
         AND department.department_type='community'
        GROUP BY community.id, community.name
        ORDER BY community.id
    """)
    result = [
        {
            "id": int(row[0]),
            "name": str(row[1]),
            "enabled": bool(row[2]),
            "sort_order": int(row[0]),
            "aliases": [],
        }
        for row in await cur.fetchall()
    ]
    by_id = {item["id"]: item for item in result}
    await cur.execute("SELECT community_id, alias FROM _community_aliases ORDER BY id")
    for community_id, alias in await cur.fetchall():
        if int(community_id) in by_id:
            by_id[int(community_id)]["aliases"].append(str(alias))
    return result


async def _address_entries(cur, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE entry.enabled=1" if enabled_only else ""
    await cur.execute(f"""
        SELECT entry.id, entry.name, entry.detail_address,
               entry.address_type, entry.pattern, entry.community_id,
               community.name, entry.aliases_json, entry.source_flags,
               entry.enabled, entry.created_at, entry.updated_at
        FROM _police_address_entries AS entry
        LEFT JOIN _communities AS community ON community.id=entry.community_id
        {where}
        ORDER BY entry.enabled DESC, community.id, entry.name, entry.id
    """)
    return [
        {
            "id": int(row[0]),
            "name": str(row[1]),
            "detail_address": str(row[2] or ""),
            "address_type": str(row[3]),
            "pattern": str(row[4] or ""),
            "community_id": int(row[5]) if row[5] is not None else None,
            "community_name": str(row[6] or ""),
            "aliases": json_value(row[7], []),
            "sources": json_value(row[8], []),
            "enabled": bool(row[9]),
            "created_at": row[10].isoformat() + "Z" if row[10] else None,
            "updated_at": row[11].isoformat() + "Z" if row[11] else None,
        }
        for row in await cur.fetchall()
    ]


def _address_payload(item: AddressCreate) -> tuple:
    aliases = sorted({alias.strip() for alias in item.aliases if alias.strip()})
    return (
        item.name.strip(),
        normalize_lookup(item.name),
        item.detail_address.strip(),
        item.address_type,
        item.pattern.strip(),
        item.community_id,
        stable_json(aliases),
        1 if item.enabled else 0,
    )


async def _assert_community(cur, community_id: int, *, require_enabled: bool = False) -> None:
    if require_enabled:
        await cur.execute("""
            SELECT community.id
            FROM _communities AS community
            JOIN _departments AS department
              ON department.community_id=community.id
             AND department.department_type='community'
             AND department.is_active=1
            WHERE community.id=%s AND community.is_active=1
            LIMIT 1
        """, (community_id,))
    else:
        await cur.execute("SELECT id FROM _communities WHERE id=%s", (community_id,))
    if not await cur.fetchone():
        raise HTTPException(400, "所选社区不存在或已停用")


@router.get("/addresses")
async def list_addresses(
    keyword: str = Query("", max_length=100),
    enabled: bool | None = Query(None),
    user: dict = Depends(require_police_address),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        data = await _address_entries(cur)
        communities = await _communities(cur)
    term = normalize_lookup(keyword)
    if term:
        data = [item for item in data if term in normalize_lookup(
            " ".join((item["name"], item["detail_address"], item["community_name"], *item["aliases"]))
        )]
    if enabled is not None:
        data = [item for item in data if item["enabled"] is enabled]
    return {"data": data, "total": len(data), "communities": communities}


@router.post("/addresses")
async def create_address(
    data: AddressCreate,
    request: Request,
    user: dict = Depends(require_police_address),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await _assert_community(cur, data.community_id, require_enabled=True)
        try:
            await cur.execute("""
                INSERT INTO _police_address_entries (
                    name, normalized_name, detail_address, address_type,
                    pattern, community_id, aliases_json, source_flags,
                    enabled, created_by, updated_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, JSON_ARRAY('manual'), %s, %s, %s)
            """, (*_address_payload(data), user["id"], user["id"]))
        except Exception as exc:
            if getattr(exc, "args", [None])[0] == 1062:
                raise HTTPException(409, "同一社区已经存在同名地址记录") from exc
            raise
        entry_id = int(cur.lastrowid)
    await record_admin_audit(
        user, "police_address.create", target_type="police_address",
        target_name=str(entry_id), detail={"address_type": data.address_type},
        **request_audit_fields(request),
    )
    return {"id": entry_id, "message": "地址记录已创建"}


@router.put("/addresses/{entry_id}")
async def update_address(
    entry_id: int,
    data: AddressCreate,
    request: Request,
    user: dict = Depends(require_police_address),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await _assert_community(cur, data.community_id, require_enabled=True)
        await cur.execute("SELECT id FROM _police_address_entries WHERE id=%s", (entry_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "地址记录不存在")
        try:
            await cur.execute("""
                UPDATE _police_address_entries SET
                    name=%s, normalized_name=%s, detail_address=%s,
                    address_type=%s, pattern=%s, community_id=%s,
                    aliases_json=%s, enabled=%s, updated_by=%s
                WHERE id=%s
            """, (*_address_payload(data), user["id"], entry_id))
        except Exception as exc:
            if getattr(exc, "args", [None])[0] == 1062:
                raise HTTPException(409, "同一社区已经存在同名地址记录") from exc
            raise
    await record_admin_audit(
        user, "police_address.update", target_type="police_address",
        target_name=str(entry_id), detail={"enabled": data.enabled},
        **request_audit_fields(request),
    )
    return {"message": "地址记录已更新"}


@router.delete("/addresses/{entry_id}")
async def disable_address(
    entry_id: int,
    request: Request,
    user: dict = Depends(require_police_address),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE _police_address_entries SET enabled=0, updated_by=%s WHERE id=%s",
            (user["id"], entry_id),
        )
        if not cur.rowcount:
            raise HTTPException(404, "地址记录不存在")
    await record_admin_audit(
        user, "police_address.disable", target_type="police_address",
        target_name=str(entry_id), **request_audit_fields(request),
    )
    return {"message": "地址记录已停用"}


def _task_counts(rows: list[tuple]) -> dict[str, int]:
    result = {
        "total": 0, "pending_review": 0, "reviewed": 0, "no_registration": 0,
        "transfer": 0, "dispatch": 0, "balanced": 0, "duplicate": 0,
        "abnormal": 0, "pending_publish": 0, "published": 0,
        "retryable": 0, "needs_reconciliation": 0, "conflict": 0,
        "cache_pending": 0,
    }
    for row in rows:
        result["total"] += int(row[0] or 0)
        result["pending_review"] += int(row[1] or 0)
        result["reviewed"] += int(row[2] or 0)
        result["no_registration"] += int(row[3] or 0)
        result["transfer"] += int(row[4] or 0)
        result["dispatch"] += int(row[5] or 0)
        result["balanced"] += int(row[6] or 0)
        result["duplicate"] += int(row[7] or 0)
        result["abnormal"] += int(row[8] or 0)
        result["pending_publish"] += int(row[9] or 0)
        result["published"] += int(row[10] or 0)
        if len(row) > 11:
            result["retryable"] += int(row[11] or 0)
            result["needs_reconciliation"] += int(row[12] or 0)
            result["conflict"] += int(row[13] or 0)
            result["cache_pending"] += int(row[14] or 0)
    return result


async def _batch_counts(cur, batch_id: int) -> dict[str, int]:
    await cur.execute("""
        SELECT COUNT(*),
               SUM(task_status='pending_review'),
               SUM(final_action<>''),
               SUM(COALESCE(NULLIF(final_action, ''), suggested_action)='no_registration'),
               SUM(COALESCE(NULLIF(final_action, ''), suggested_action)='transfer'),
               SUM(COALESCE(NULLIF(final_action, ''), suggested_action)='dispatch'),
               SUM(allocation_mode='balanced'),
               SUM(duplicate_group_key<>''),
               SUM(suggested_action='manual'),
               SUM(publish_status IN ('pending', 'publishing', 'retryable',
                                      'needs_reconciliation', 'conflict')),
               SUM(publish_status='success'),
               SUM(publish_status='retryable'),
               SUM(publish_status='needs_reconciliation'),
               SUM(publish_status='conflict'),
               SUM(cache_pending=1)
        FROM _police_dispatch_tasks WHERE batch_id=%s
    """, (batch_id,))
    return _task_counts([await cur.fetchone()])


async def _refresh_batch_status(cur, batch_id: int) -> dict[str, int]:
    counts = await _batch_counts(cur, batch_id)
    if counts["total"] and counts["pending_review"] == 0:
        if counts["pending_publish"] == 0:
            status = "completed"
        elif counts["needs_reconciliation"] or counts["conflict"]:
            status = "reconciling"
        else:
            status = "ready_to_publish"
    else:
        status = "reviewing"
    await cur.execute("""
        UPDATE _police_dispatch_batches SET status=%s, counts_json=%s,
            completed_at=CASE WHEN %s='completed' THEN COALESCE(completed_at, UTC_TIMESTAMP()) ELSE NULL END
        WHERE id=%s
    """, (status, stable_json(counts), status, batch_id))
    return counts


async def _batch_payload(cur, batch_id: int) -> dict[str, Any]:
    payloads = await _batch_payloads(cur, [batch_id])
    if not payloads:
        raise HTTPException(404, "批次不存在")
    return payloads[0]


async def _batch_payloads(cur, batch_ids: list[int]) -> list[dict[str, Any]]:
    if not batch_ids:
        return []
    placeholders = ",".join(["%s"] * len(batch_ids))
    await cur.execute(f"""
        SELECT batch.id, batch.file_name, batch.sheet_name, batch.status,
               batch.total_count, batch.counts_json, batch.first_publish_date,
               batch.last_error, batch.created_at, batch.updated_at,
               user.display_name, user.username
        FROM _police_dispatch_batches AS batch
        LEFT JOIN _users AS user ON user.id=batch.imported_by
        WHERE batch.id IN ({placeholders})
    """, batch_ids)
    batch_rows = {int(row[0]): row for row in await cur.fetchall()}
    await cur.execute(f"""
        SELECT batch_id, COUNT(*),
               SUM(task_status='pending_review'),
               SUM(final_action<>''),
               SUM(COALESCE(NULLIF(final_action, ''), suggested_action)='no_registration'),
               SUM(COALESCE(NULLIF(final_action, ''), suggested_action)='transfer'),
               SUM(COALESCE(NULLIF(final_action, ''), suggested_action)='dispatch'),
               SUM(allocation_mode='balanced'),
               SUM(duplicate_group_key<>''),
               SUM(suggested_action='manual'),
               SUM(publish_status IN ('pending', 'publishing', 'retryable',
                                      'needs_reconciliation', 'conflict')),
               SUM(publish_status='success'),
               SUM(publish_status='retryable'),
               SUM(publish_status='needs_reconciliation'),
               SUM(publish_status='conflict'),
               SUM(cache_pending=1)
        FROM _police_dispatch_tasks
        WHERE batch_id IN ({placeholders}) GROUP BY batch_id
    """, batch_ids)
    counts_by_batch = {
        int(row[0]): _task_counts([row[1:]])
        for row in await cur.fetchall()
    }
    await cur.execute(f"""
        SELECT task.batch_id, community.id, community.name, COUNT(*)
        FROM _police_dispatch_tasks AS task
        JOIN _communities AS community
          ON community.id=COALESCE(task.final_community_id, task.suggested_community_id)
        WHERE task.batch_id IN ({placeholders})
          AND COALESCE(NULLIF(task.final_action, ''), task.suggested_action)='dispatch'
        GROUP BY task.batch_id, community.id, community.name
        ORDER BY task.batch_id, community.id
    """, batch_ids)
    distribution_by_batch: dict[int, list[dict[str, Any]]] = {}
    for batch_id, community_id, community_name, count in await cur.fetchall():
        distribution_by_batch.setdefault(int(batch_id), []).append({
            "community_id": int(community_id),
            "community_name": str(community_name),
            "count": int(count),
        })
    result = []
    for batch_id in batch_ids:
        row = batch_rows.get(int(batch_id))
        if not row:
            continue
        counts = counts_by_batch.get(int(batch_id), _task_counts([]))
        result.append({
            "id": int(row[0]), "file_name": str(row[1]), "sheet_name": str(row[2]),
            "status": str(row[3]), "total_count": int(row[4]), "counts": counts,
            "first_publish_date": row[6].isoformat() if row[6] else None,
            "last_error": str(row[7] or ""),
            "created_at": row[8].isoformat() + "Z", "updated_at": row[9].isoformat() + "Z",
            "imported_by": str(row[10] or row[11] or ""),
            "reviewed_count": counts["reviewed"],
            "community_distribution": distribution_by_batch.get(int(batch_id), []),
        })
    return result


@router.post("/batches")
async def upload_dispatch_batch(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    filename, content = await _read_upload(file)
    digest = sha256(content).hexdigest()
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM _police_dispatch_batches WHERE file_sha256=%s", (digest,))
        duplicate = await cur.fetchone()
        if duplicate:
            payload = await _batch_payload(cur, int(duplicate[0]))
            return {"status": "duplicate", "message": "同一文件已经导入", "batch": payload}
    try:
        sheet_name, parsed = await asyncio.to_thread(parse_dispatch_workbook, content, filename)
    except MemoryError as exc:
        raise HTTPException(
            413,
            "工作簿展开后占用内存过大，请拆分文件后重新上传",
        ) from exc
    except PoliceWorkbookError as exc:
        raise HTTPException(400, str(exc)) from exc
    async with conn.cursor() as cur:
        communities = await _communities(cur)
        addresses = await _address_entries(cur, enabled_only=True)
    tasks = [
        {
            "source_row": item.source_row, "source_name": item.source_name,
            "person_name": item.person_name, "identity_number": item.identity_number,
            "phone": item.phone, "original_address": item.original_address,
            "created_time": item.created_time, "transfer_note": item.transfer_note,
            "raw_values": item.raw_values,
        }
        for item in parsed
    ]
    apply_preprocessing_suggestions(tasks, communities, addresses)
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO _police_dispatch_batches (
                    file_name, file_sha256, sheet_name, status,
                    total_count, counts_json, imported_by
                ) VALUES (%s, %s, %s, 'reviewing', %s, JSON_OBJECT(), %s)
            """, (filename, digest, sheet_name, len(tasks), user["id"]))
            batch_id = int(cur.lastrowid)
            for item in tasks:
                await cur.execute("""
                    INSERT INTO _police_dispatch_tasks (
                        batch_id, source_row, source_name, person_name,
                        identity_number, identity_hash, phone, original_address,
                        source_created_time, transfer_note, raw_values_json,
                        duplicate_group_key, duplicate_kind, suggested_action,
                        suggested_community_id, suggestion_reason, allocation_mode
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                """, (
                    batch_id, item["source_row"], item["source_name"], item["person_name"],
                    item["identity_number"], item.get("identity_hash", ""), item["phone"],
                    item["original_address"], item["created_time"], item["transfer_note"],
                    stable_json(item["raw_values"]), item.get("duplicate_group_key", ""),
                    item.get("duplicate_kind", ""), item["suggested_action"],
                    item.get("suggested_community_id"), item["suggestion_reason"],
                    item["allocation_mode"],
                ))
            await _refresh_batch_status(cur, batch_id)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    async with conn.cursor() as cur:
        payload = await _batch_payload(cur, batch_id)
    await record_admin_audit(
        user, "police_dispatch.import", target_type="police_dispatch_batch",
        target_name=str(batch_id), detail={"row_count": len(tasks)},
        **request_audit_fields(request),
    )
    return {"status": "success", "message": "文件已导入，所有建议等待人工审核", "batch": payload}


@router.get("/batches")
async def list_batches(
    file_name: str = Query("", max_length=100),
    upload_date: str = Query("", max_length=10),
    status: str = Query("all", max_length=30),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    del user
    where = ["1=1"]
    params: list[Any] = []
    if file_name.strip():
        where.append("file_name LIKE %s")
        params.append(f"%{file_name.strip()}%")
    if upload_date.strip():
        try:
            datetime.strptime(upload_date.strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(400, "上传日期格式必须为 YYYY-MM-DD") from exc
        where.append("DATE(created_at)=%s")
        params.append(upload_date.strip())
    if status != "all":
        allowed = {"reviewing", "ready_to_publish", "publishing", "reconciling", "completed"}
        if status not in allowed:
            raise HTTPException(400, "批次状态筛选无效")
        where.append("status=%s")
        params.append(status)
    where_sql = " AND ".join(where)
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT COUNT(*) FROM _police_dispatch_batches WHERE {where_sql}",
            params,
        )
        total = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            f"""
            SELECT id FROM _police_dispatch_batches
            WHERE {where_sql}
            ORDER BY (status='completed') ASC,
                     CASE WHEN status<>'completed' THEN created_at END ASC,
                     CASE WHEN status='completed' THEN created_at END DESC,
                     id DESC
            LIMIT %s OFFSET %s
            """,
            params + [page_size, (page - 1) * page_size],
        )
        ids = [int(row[0]) for row in await cur.fetchall()]
        data = await _batch_payloads(cur, ids)
    return {"data": data, "total": total, "page": page, "page_size": page_size}


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: int,
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        batch = await _batch_payload(cur, batch_id)
        communities = await _communities(cur)
    return {"batch": batch, "communities": communities}


def _task_payload(row: tuple) -> dict[str, Any]:
    raw_values = json_value(row[27], {}) if len(row) > 27 else {}
    requested_values = json_value(row[32], {}) if len(row) > 32 else {}
    conflict_values = json_value(row[30], {}) if len(row) > 30 else {}
    conflict_diff = [
        {
            "field": field,
            "platform": str(requested_values.get(field, "") or ""),
            "tencent": str(conflict_values.get(field, "") or ""),
        }
        for field in sorted(set(requested_values) | set(conflict_values))
        if str(requested_values.get(field, "") or "")
        != str(conflict_values.get(field, "") or "")
    ]
    return {
        "id": int(row[0]), "batch_id": int(row[1]), "source_row": int(row[2]),
        "source_name": str(row[3] or ""), "person_name": str(row[4] or ""),
        "identity_number": str(row[5] or ""), "phone": str(row[6] or ""),
        "original_address": str(row[7] or ""), "created_time": str(row[8] or ""),
        "transfer_note": str(row[9] or ""), "duplicate_group_key": str(row[10] or ""),
        "duplicate_kind": str(row[11] or ""), "suggested_action": str(row[12] or ""),
        "suggested_community_id": int(row[13]) if row[13] else None,
        "suggested_community_name": str(row[14] or ""),
        "suggestion_reason": str(row[15] or ""), "allocation_mode": str(row[16] or ""),
        "final_action": str(row[17] or ""),
        "final_community_id": int(row[18]) if row[18] else None,
        "final_community_name": str(row[19] or ""), "review_note": str(row[20] or ""),
        "reviewer_name": str(row[21] or ""),
        "reviewed_at": row[22].isoformat() + "Z" if row[22] else None,
        "version": int(row[23]), "task_status": str(row[24]),
        "publish_status": str(row[25]), "publish_error": str(row[26] or ""),
        "raw_values": raw_values,
        "field_roles": dispatch_field_roles(raw_values),
        "linked_source_id": int(row[28]) if len(row) > 28 and row[28] else None,
        "linked_row_hash": str(row[29] or "") if len(row) > 29 else "",
        "conflict_values": conflict_values,
        "requested_values": requested_values,
        "conflict_diff": conflict_diff,
        "cache_pending": bool(row[31]) if len(row) > 31 else False,
    }


TASK_SELECT = """
    SELECT task.id, task.batch_id, task.source_row, task.source_name,
           task.person_name, task.identity_number, task.phone,
           task.original_address, task.source_created_time,
           task.transfer_note, task.duplicate_group_key, task.duplicate_kind,
           task.suggested_action, task.suggested_community_id,
           suggested.name, task.suggestion_reason, task.allocation_mode,
           task.final_action, task.final_community_id, final.name,
           task.review_note, task.reviewer_name, task.reviewed_at,
           task.version, task.task_status, task.publish_status,
           task.publish_error, task.raw_values_json, task.linked_source_id,
           task.linked_row_hash, task.conflict_values_json,
           task.cache_pending, result.request_values_json
    FROM _police_dispatch_tasks AS task
    LEFT JOIN _communities AS suggested ON suggested.id=task.suggested_community_id
    LEFT JOIN _communities AS final ON final.id=task.final_community_id
    LEFT JOIN _police_dispatch_publish_results AS result ON result.task_id=task.id
"""


async def _search_tasks(cur, search: TaskSearch) -> dict[str, Any]:
    where = ["task.batch_id=%s"]
    params: list[Any] = [search.batch_id]
    if search.status == "pending_review":
        where.append("task.task_status='pending_review'")
    elif search.status == "pending_publish":
        where.append("task.publish_status IN ('pending', 'publishing')")
    elif search.status == "retryable":
        where.append("task.publish_status='retryable'")
    elif search.status == "needs_reconciliation":
        where.append("task.publish_status='needs_reconciliation'")
    elif search.status == "conflict":
        where.append("task.publish_status='conflict'")
    elif search.status == "completed":
        where.append("task.task_status='completed'")
    elif search.status != "all":
        raise HTTPException(400, "任务状态筛选无效")
    if search.category != "all":
        if search.category == "duplicate":
            where.append("task.duplicate_group_key<>''")
        elif search.category == "balanced":
            where.append("task.allocation_mode='balanced'")
        elif search.category in {"dispatch", "no_registration", "transfer", "manual"}:
            where.append("COALESCE(NULLIF(task.final_action, ''), task.suggested_action)=%s")
            params.append(search.category)
        else:
            raise HTTPException(400, "任务分类筛选无效")
    if search.keyword.strip():
        where.append("CONCAT_WS(' ', task.person_name, task.identity_number, task.phone, task.original_address) LIKE %s")
        params.append(f"%{search.keyword.strip()}%")
    where_sql = " AND ".join(where)
    await cur.execute(f"SELECT COUNT(*) FROM _police_dispatch_tasks AS task WHERE {where_sql}", params)
    total = int((await cur.fetchone())[0] or 0)
    await cur.execute(
        f"{TASK_SELECT} WHERE {where_sql} ORDER BY task.source_row, task.id LIMIT %s OFFSET %s",
        params + [search.page_size, (search.page - 1) * search.page_size],
    )
    data = [_task_payload(row) for row in await cur.fetchall()]
    return {"data": data, "total": total, "page": search.page, "page_size": search.page_size}


@router.get("/tasks")
async def list_tasks(
    request: Request,
    batch_id: int = Query(...),
    status: str = Query("all", max_length=30),
    category: str = Query("all", max_length=30),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    del user
    if "keyword" in request.query_params:
        raise HTTPException(400, "敏感关键词必须通过 POST 搜索接口提交")
    async with conn.cursor() as cur:
        return await _search_tasks(cur, TaskSearch(
            batch_id=batch_id, status=status, category=category,
            page=page, page_size=page_size,
        ))


@router.post("/tasks/search")
async def search_tasks(
    data: TaskSearch,
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        return await _search_tasks(cur, data)


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: int,
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute(f"{TASK_SELECT} WHERE task.id=%s", (task_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        task = _task_payload(row)
        siblings = []
        if task["duplicate_group_key"]:
            await cur.execute(
                f"{TASK_SELECT} WHERE task.batch_id=%s AND task.duplicate_group_key=%s ORDER BY task.source_row",
                (task["batch_id"], task["duplicate_group_key"]),
            )
            siblings = [_task_payload(item) for item in await cur.fetchall()]
        communities = await _communities(cur)
    duplicate_differences = []
    if len(siblings) > 1:
        headers = sorted({
            header
            for sibling in siblings
            for header in sibling.get("raw_values", {})
        })
        differing_headers = [
            header for header in headers
            if len({str(item.get("raw_values", {}).get(header, "")) for item in siblings}) > 1
        ]
        duplicate_differences = [
            {
                "task_id": item["id"],
                "source_row": item["source_row"],
                "fields": [
                    {"field": header, "value": str(item.get("raw_values", {}).get(header, ""))}
                    for header in differing_headers
                ],
            }
            for item in siblings
        ]
    return {
        "task": task,
        "duplicates": siblings,
        "duplicate_differences": duplicate_differences,
        "communities": communities,
    }


async def _review_one(cur, task_id: int, data: TaskReview, user: dict) -> int:
    await cur.execute("""
        SELECT batch_id, version, publish_status, duplicate_group_key,
               person_name, identity_number, original_address
        FROM _police_dispatch_tasks
        WHERE id=%s FOR UPDATE
    """, (task_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    if int(row[1]) != data.expected_version:
        raise HTTPException(409, "任务已被其他人修改，请刷新后重试")
    if str(row[2]) in {"success", "publishing", "needs_reconciliation", "conflict"}:
        raise HTTPException(409, "任务已写入或正在对账，不能直接重新审核")
    if data.final_action == "duplicate_exclude" and not str(row[3] or ""):
        raise HTTPException(400, "只有同批重复人员记录才能标记为重复排除")
    community_id = data.final_community_id if data.final_action == "dispatch" else None
    if data.final_action == "dispatch":
        missing = [
            label for value, label in (
                (row[4] if len(row) > 4 else "", "姓名"),
                (row[5] if len(row) > 5 else "", "身份证号"),
                (row[6] if len(row) > 6 else "", "地址"),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise HTTPException(400, f"下发前必须补齐：{'、'.join(missing)}")
        if not community_id:
            raise HTTPException(400, "下发任务必须选择社区")
        await _assert_community(cur, community_id, require_enabled=True)
    task_status = "pending_publish" if data.final_action == "dispatch" else "completed"
    publish_status = "pending" if data.final_action == "dispatch" else "not_required"
    reviewer_name = str(user.get("display_name") or (user.get("member") or {}).get("name") or user.get("username") or "")
    await cur.execute("""
        UPDATE _police_dispatch_tasks SET
            final_action=%s, final_community_id=%s, review_note=%s,
            reviewed_by=%s, reviewer_name=%s, reviewed_at=UTC_TIMESTAMP(),
            version=version+1, task_status=%s, publish_status=%s,
            publish_error=''
        WHERE id=%s AND version=%s
    """, (
        data.final_action, community_id, data.review_note.strip(), user["id"],
        reviewer_name[:100], task_status, publish_status, task_id, data.expected_version,
    ))
    if cur.rowcount != 1:
        raise HTTPException(409, "任务已被其他人修改，请刷新后重试")
    return int(row[0])


async def _recalculate_batch_tasks(cur, batch_id: int, edited_task_id: int) -> set[int]:
    """重新计算整批建议、平均分配和重复关系，并清除受影响审核。"""
    await cur.execute("""
        SELECT id, source_row, source_name, person_name, identity_number,
               phone, original_address, source_created_time, transfer_note,
               raw_values_json, duplicate_group_key, duplicate_kind,
               suggested_action, suggested_community_id, suggestion_reason,
               allocation_mode, final_action, publish_status
        FROM _police_dispatch_tasks
        WHERE batch_id=%s ORDER BY source_row, id FOR UPDATE
    """, (batch_id,))
    loaded = await cur.fetchall()
    rows: list[dict[str, Any]] = []
    originals: dict[int, tuple[Any, ...]] = {}
    for item in loaded:
        task_id = int(item[0])
        row = {
            "id": task_id,
            "source_row": int(item[1]),
            "source_name": str(item[2] or ""),
            "person_name": str(item[3] or ""),
            "identity_number": str(item[4] or ""),
            "phone": str(item[5] or ""),
            "original_address": str(item[6] or ""),
            "created_time": str(item[7] or ""),
            "transfer_note": str(item[8] or ""),
            "raw_values": json_value(item[9], {}),
            "duplicate_group_key": "",
            "duplicate_kind": "",
        }
        rows.append(row)
        originals[task_id] = (
            str(item[10] or ""), str(item[11] or ""), str(item[12] or ""),
            int(item[13]) if item[13] else None, str(item[14] or ""),
            str(item[15] or ""), str(item[16] or ""), str(item[17] or ""),
        )
    communities = await _communities(cur)
    addresses = await _address_entries(cur, enabled_only=True)
    apply_preprocessing_suggestions(rows, communities, addresses)
    affected: set[int] = {edited_task_id}
    for row in rows:
        task_id = int(row["id"])
        previous = originals[task_id]
        derived = (
            str(row.get("duplicate_group_key") or ""),
            str(row.get("duplicate_kind") or ""),
            str(row.get("suggested_action") or ""),
            int(row["suggested_community_id"]) if row.get("suggested_community_id") else None,
            str(row.get("suggestion_reason") or ""),
            str(row.get("allocation_mode") or ""),
        )
        if derived != previous[:6]:
            affected.add(task_id)
        published = previous[7] == "success"
        clear_review = task_id in affected and not published
        await cur.execute("""
            UPDATE _police_dispatch_tasks SET
                identity_hash=%s, duplicate_group_key=%s, duplicate_kind=%s,
                suggested_action=%s, suggested_community_id=%s,
                suggestion_reason=%s, allocation_mode=%s,
                final_action=CASE WHEN %s THEN '' ELSE final_action END,
                final_community_id=CASE WHEN %s THEN NULL ELSE final_community_id END,
                review_note=CASE WHEN %s THEN '' ELSE review_note END,
                reviewed_by=CASE WHEN %s THEN NULL ELSE reviewed_by END,
                reviewer_name=CASE WHEN %s THEN '' ELSE reviewer_name END,
                reviewed_at=CASE WHEN %s THEN NULL ELSE reviewed_at END,
                task_status=CASE WHEN %s THEN 'pending_review' ELSE task_status END,
                publish_status=CASE WHEN %s THEN 'not_required' ELSE publish_status END,
                publish_error=CASE WHEN %s THEN '' ELSE publish_error END,
                version=version+CASE WHEN %s THEN 1 ELSE 0 END
            WHERE id=%s
        """, (
            identity_digest(row.get("identity_number", "")),
            derived[0], derived[1], derived[2], derived[3], derived[4], derived[5],
            clear_review, clear_review, clear_review, clear_review, clear_review,
            clear_review, clear_review, clear_review, clear_review, clear_review,
            task_id,
        ))
    return affected


@router.patch("/tasks/{task_id}/business-fields")
async def update_task_business_fields(
    task_id: int,
    data: TaskBusinessFieldsUpdate,
    request: Request,
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    if not data.fields:
        raise HTTPException(400, "没有提交需要修改的业务字段")
    if any(len(value) > 5000 for value in data.fields.values()):
        raise HTTPException(400, "单个业务字段不能超过 5000 个字符")
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT batch_id, version, publish_status, raw_values_json
                FROM _police_dispatch_tasks WHERE id=%s FOR UPDATE
            """, (task_id,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "任务不存在")
            if int(row[1]) != data.expected_version:
                raise HTTPException(409, "任务已被其他人修改，请刷新后重试")
            if str(row[2]) in {"success", "publishing", "needs_reconciliation", "conflict"}:
                raise HTTPException(409, "已写入或正在对账的任务不能修改业务字段")
            raw_values = json_value(row[3], {})
            unknown = sorted(set(data.fields) - set(raw_values))
            if unknown:
                raise HTTPException(400, f"不能修改不存在的导入字段：{'、'.join(unknown[:5])}")
            changed = {
                field: value.strip()
                for field, value in data.fields.items()
                if str(raw_values.get(field, "")) != value.strip()
            }
            if not changed:
                await conn.rollback()
                return {"message": "业务字段没有变化", "version": data.expected_version}
            raw_values.update(changed)
            extracted = dispatch_values_from_raw(raw_values)
            await cur.execute("""
                UPDATE _police_dispatch_tasks SET
                    source_name=%s, person_name=%s, identity_number=%s,
                    identity_hash=%s, phone=%s, original_address=%s,
                    source_created_time=%s, transfer_note=%s,
                    raw_values_json=%s
                WHERE id=%s AND version=%s
            """, (
                extracted["source_name"], extracted["person_name"],
                extracted["identity_number"], identity_digest(extracted["identity_number"]),
                extracted["phone"], extracted["original_address"],
                extracted["created_time"], extracted["transfer_note"],
                stable_json(extracted["raw_values"]), task_id, data.expected_version,
            ))
            if cur.rowcount != 1:
                raise HTTPException(409, "任务已被其他人修改，请刷新后重试")
            batch_id = int(row[0])
            affected = await _recalculate_batch_tasks(cur, batch_id, task_id)
            await _refresh_batch_status(cur, batch_id)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "police_dispatch.business_fields.update",
        target_type="police_dispatch_task", target_name=str(task_id),
        detail={
            "changed_fields": sorted(changed),
            "change_digest": sha256(stable_json(changed).encode("utf-8")).hexdigest(),
            "affected_count": len(affected),
        },
        **request_audit_fields(request),
    )
    return {
        "message": "业务字段已保存，相关建议和审核状态已重新计算",
        "affected_count": len(affected),
    }


@router.patch("/tasks/{task_id}")
async def review_task(
    task_id: int,
    data: TaskReview,
    request: Request,
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            batch_id = await _review_one(cur, task_id, data, user)
            await _refresh_batch_status(cur, batch_id)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "police_dispatch.review", target_type="police_dispatch_task",
        target_name=str(task_id), detail={"action": data.final_action, "batch_id": batch_id},
        **request_audit_fields(request),
    )
    return {"message": "审核结果已保存", "version": data.expected_version + 1}


@router.post("/tasks/bulk-review")
async def bulk_review_tasks(
    data: BulkReview,
    request: Request,
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    ids = [int(item.get("id") or 0) for item in data.tasks]
    versions = {int(item.get("id") or 0): int(item.get("version") or 0) for item in data.tasks}
    if len(set(ids)) != len(ids) or any(item <= 0 for item in ids):
        raise HTTPException(400, "批量任务列表无效")
    placeholders = ",".join(["%s"] * len(ids))
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT id, batch_id, version, suggested_action, suggested_community_id "
                f"FROM _police_dispatch_tasks WHERE id IN ({placeholders}) FOR UPDATE",
                ids,
            )
            rows = await cur.fetchall()
            if len(rows) != len(ids):
                raise HTTPException(404, "部分任务不存在")
            batch_ids = {int(row[1]) for row in rows}
            if len(batch_ids) != 1:
                raise HTTPException(400, "一次只能批量审核同一批次")
            for row in rows:
                task_id, _, version, suggested_action, suggested_community_id = row
                if int(version) != versions[int(task_id)]:
                    raise HTTPException(409, "部分任务已被其他人修改，请刷新后重试")
                action = str(suggested_action) if data.mode == "accept_suggestion" else data.final_action
                community_id = int(suggested_community_id) if suggested_community_id else None
                if data.mode == "set_action":
                    community_id = data.final_community_id
                if action not in FINAL_ACTIONS:
                    raise HTTPException(400, "筛选结果中含有必须逐条人工判断的任务")
                await _review_one(cur, int(task_id), TaskReview(
                    expected_version=int(version), final_action=action,
                    final_community_id=community_id, review_note=data.review_note,
                ), user)
            batch_id = next(iter(batch_ids))
            await _refresh_batch_status(cur, batch_id)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "police_dispatch.bulk_review", target_type="police_dispatch_batch",
        target_name=str(batch_id), detail={"count": len(ids), "mode": data.mode},
        **request_audit_fields(request),
    )
    return {"message": f"已审核 {len(ids)} 条任务", "count": len(ids)}


@router.post("/tasks/{task_id}/resolve-conflict")
async def resolve_publish_conflict(
    task_id: int,
    data: ConflictResolution,
    request: Request,
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    parser = get_parser("全链条")
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT task.batch_id, task.version, task.publish_status,
                   task.linked_source_id, task.linked_row_hash,
                   task.raw_values_json, result.request_values_json,
                   result.spreadsheet_id, result.physical_row
            FROM _police_dispatch_tasks AS task
            JOIN _police_dispatch_publish_results AS result
              ON result.task_id=task.id
            WHERE task.id=%s
        """, (task_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "冲突任务不存在")
        if int(row[1]) != data.expected_version:
            raise HTTPException(409, "任务已变化，请刷新冲突详情")
        if str(row[2]) != "conflict":
            raise HTTPException(409, "任务当前不处于内容冲突状态")
        if str(row[4] or "") != data.expected_row_hash:
            raise HTTPException(409, "腾讯来源行已变化，请等待同步后刷新")
        if not row[3]:
            raise HTTPException(409, "尚未定位腾讯来源行，请等待一次正常同步")
        source = await _load_source_row(cur, "全链条", int(row[3]))
        if source["row_hash"] != data.expected_row_hash:
            raise HTTPException(409, "腾讯来源行已变化，请刷新后重新选择")
        batch_id = int(row[0])
        platform_values = json_value(row[6], {})

    if data.strategy == "adopt_tencent":
        values = {
            column: str(source["values"].get(column, "") or "").strip()
            for column in parser.COLUMNS
        }
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT version, linked_row_hash FROM _police_dispatch_tasks "
                    "WHERE id=%s FOR UPDATE",
                    (task_id,),
                )
                locked = await cur.fetchone()
                if not locked or int(locked[0]) != data.expected_version \
                        or str(locked[1] or "") != data.expected_row_hash:
                    raise HTTPException(409, "冲突任务已变化，请刷新后重试")
                communities = await _communities(cur)
                community = resolve_community(
                    values.get("社区", ""), community_resolver(communities),
                )
                if not community or not community.get("enabled", False):
                    raise HTTPException(409, "腾讯内容中的社区无法映射为启用中的正式社区")
                raw_values = json_value(row[5], {})
                roles = dispatch_field_roles(raw_values)
                replacements = {
                    "source": values.get("来源", ""),
                    "name": values.get("姓名", ""),
                    "identity": values.get("身份证号", ""),
                    "phone": values.get("电话号码", ""),
                    "address": values.get("地址", ""),
                    "created": values.get("创建时间", ""),
                    "transfer_note": "",
                }
                for field, value in replacements.items():
                    header = roles.get(field)
                    if header:
                        raw_values[header] = value
                await cur.execute("""
                    UPDATE _police_dispatch_tasks SET
                        source_name=%s, person_name=%s, identity_number=%s,
                        identity_hash=%s, phone=%s, original_address=%s,
                        source_created_time=%s, transfer_note='', raw_values_json=%s,
                        final_action='dispatch', final_community_id=%s,
                        suggested_action='dispatch', suggested_community_id=%s,
                        suggestion_reason='已采用腾讯现有内容', allocation_mode='matched',
                        publish_status='success', task_status='completed',
                        publish_error='', conflict_values_json=NULL,
                        cache_pending=0, published_at=COALESCE(published_at, UTC_TIMESTAMP()),
                        version=version+1
                    WHERE id=%s AND version=%s
                """, (
                    replacements["source"], replacements["name"],
                    replacements["identity"], identity_digest(replacements["identity"]),
                    replacements["phone"], replacements["address"],
                    replacements["created"], stable_json(raw_values), community["id"],
                    community["id"], task_id, data.expected_version,
                ))
                if cur.rowcount != 1:
                    raise HTTPException(409, "任务已变化，请刷新后重试")
                await cur.execute("""
                    UPDATE _police_dispatch_publish_results SET
                        status='success', resolution='adopt_tencent',
                        verified_values_json=%s, expected_row_hash=%s,
                        error_code='', error_message='', cache_pending=0
                    WHERE task_id=%s
                """, (stable_json(values), data.expected_row_hash, task_id))
                await _recalculate_batch_tasks(cur, batch_id, task_id)
                await _refresh_batch_status(cur, batch_id)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    else:
        if data.confirmation != "覆盖腾讯内容":
            raise HTTPException(400, "请输入“覆盖腾讯内容”完成二次确认")
        async with conn.cursor() as cur:
            if not await _writeback_enabled(cur):
                raise HTTPException(503, "在线回写已由超级管理员暂停")
            spreadsheets = await _enabled_spreadsheets(cur, "全链条")
            spreadsheet = next(
                (item for item in spreadsheets if item["id"] == int(row[7])),
                None,
            )
            if not spreadsheet:
                raise HTTPException(409, "原腾讯来源表已停用，不能覆盖")
            if not await acquire_sheet_lock(cur, spreadsheet["id"], timeout=2):
                raise HTTPException(409, "全链条表格正在同步或被他人编辑，请稍后重试")
        client = None
        cache_pending = False
        request_sent = False
        requested: dict[str, str] = {}
        try:
            async with conn.cursor() as cur:
                client = await _oauth_client(cur)
            live = await client.read_source_row(
                spreadsheet["file_id"], spreadsheet["data_sheet_id"],
                int(row[8]), parser.COLUMNS,
            )
            live_values = {
                column: str(live["values"].get(column, "") or "").strip()
                for column in parser.COLUMNS
            }
            if source_row_hash(live_values) != data.expected_row_hash:
                raise HTTPException(409, "腾讯行在确认后再次变化，请刷新冲突详情")
            requested = {
                column: str(platform_values.get(column, "") or "").strip()
                for column in parser.COLUMNS
            }
            request_sent = True
            await client.batch_update(
                spreadsheet["file_id"],
                [client.build_update_range_request(
                    spreadsheet["data_sheet_id"], int(row[8]) - 1, 0,
                    [[requested[column] for column in parser.COLUMNS]],
                )],
            )
            verified = await client.read_source_row(
                spreadsheet["file_id"], spreadsheet["data_sheet_id"],
                int(row[8]), parser.COLUMNS,
            )
            if not _row_values_match(
                requested, verified["values"], verified.get("cell_meta") or {},
                parser.COLUMNS,
            ):
                raise HTTPException(502, "腾讯覆盖后回读不一致")
            verified_values = {
                column: str(verified["values"].get(column, "") or "").strip()
                for column in parser.COLUMNS
            }
            verified_hash = source_row_hash(verified_values)
            try:
                await _refresh_spreadsheet(conn, client, spreadsheet)
            except Exception:
                cache_pending = True
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        UPDATE _police_dispatch_tasks SET
                            publish_status='success', task_status='completed',
                            publish_error='', linked_row_hash=%s,
                            conflict_values_json=NULL, cache_pending=%s,
                            published_at=COALESCE(published_at, UTC_TIMESTAMP()),
                            version=version+1
                        WHERE id=%s AND version=%s AND publish_status='conflict'
                    """, (
                        verified_hash, 1 if cache_pending else 0,
                        task_id, data.expected_version,
                    ))
                    if cur.rowcount != 1:
                        raise HTTPException(409, "任务已变化，请刷新后重试")
                    await cur.execute("""
                        UPDATE _police_dispatch_publish_results SET
                            status='success', resolution='overwrite_tencent',
                            verified_values_json=%s, expected_row_hash=%s,
                            error_code='', error_message='', cache_pending=%s
                        WHERE task_id=%s
                    """, (
                        stable_json(verified_values), verified_hash,
                        1 if cache_pending else 0, task_id,
                    ))
                    await _refresh_batch_status(cur, batch_id)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        except Exception as exc:
            if request_sent and requested:
                safe_error = "腾讯覆盖请求结果尚未确认，等待下次正常同步对账"
                async with conn.cursor() as cur:
                    marked_for_reconciliation = await _mark_overwrite_uncertain(
                        cur,
                        task_id=task_id,
                        batch_id=batch_id,
                        spreadsheet=spreadsheet,
                        physical_row=int(row[8]),
                        requested=requested,
                        error=safe_error,
                    )
                if marked_for_reconciliation:
                    raise HTTPException(502, safe_error) from exc
            raise
        finally:
            try:
                if client:
                    await client.close()
            finally:
                async with conn.cursor() as cur:
                    await release_sheet_lock(cur, spreadsheet["id"])

    changed_fields = sorted({
        column for column in parser.COLUMNS
        if str(platform_values.get(column, "") or "")
        != str(source["values"].get(column, "") or "")
    })
    await record_admin_audit(
        user, f"police_dispatch.conflict.{data.strategy}",
        target_type="police_dispatch_task", target_name=str(task_id),
        detail={
            "changed_fields": changed_fields,
            "row_hash": data.expected_row_hash,
        },
        **request_audit_fields(request),
    )
    return {
        "message": "已采用腾讯内容" if data.strategy == "adopt_tencent" else "已用平台内容覆盖腾讯现有行",
        "cache_pending": cache_pending if data.strategy == "overwrite_tencent" else False,
    }


@router.get("/workbench/home")
async def workbench_home(
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT id FROM _police_dispatch_batches
            WHERE status<>'completed'
            ORDER BY created_at, id LIMIT 1
        """)
        active_row = await cur.fetchone()
        await cur.execute("""
            SELECT id FROM _police_dispatch_batches
            ORDER BY created_at DESC, id DESC LIMIT 8
        """)
        ids = [int(row[0]) for row in await cur.fetchall()]
        if active_row and int(active_row[0]) not in ids:
            ids.insert(0, int(active_row[0]))
        batches = await _batch_payloads(cur, ids)
        communities = await _communities(cur)
    active_id = int(active_row[0]) if active_row else None
    active = next((item for item in batches if item["id"] == active_id), None)
    if active is None and batches:
        active = batches[0]
    return {"active_batch": active, "batches": batches, "communities": communities}


@router.get("/batches/{batch_id}/feedback.xlsx")
async def export_feedback(
    batch_id: int,
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        batch = await _batch_payload(cur, batch_id)
        await cur.execute("""
            SELECT source_row, source_name, person_name, identity_number,
                   phone, original_address, final_action, review_note,
                   suggestion_reason, reviewer_name, reviewed_at
            FROM _police_dispatch_tasks
            WHERE batch_id=%s AND final_action IN ('no_registration', 'transfer')
            ORDER BY source_row
        """, (batch_id,))
        tasks = [
            {
                "source_row": row[0], "source_name": row[1], "person_name": row[2],
                "identity_number": row[3], "phone": row[4], "original_address": row[5],
                "final_action": row[6], "review_note": row[7], "suggestion_reason": row[8],
                "reviewer_name": row[9],
                "reviewed_at_text": row[10].strftime("%Y-%m-%d %H:%M:%S") if row[10] else "",
            }
            for row in await cur.fetchall()
        ]
    content = await asyncio.to_thread(build_feedback_workbook, batch, tasks, datetime.now())
    filename = f"下发批次-{batch_id}-反馈.xlsx"
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _publish_values(task: dict[str, Any], community: str, publish_date) -> dict[str, str]:
    deadline = publish_date + timedelta(days=3)
    return {
        "下发日期": publish_date.strftime("%m-%d"),
        "截止日期": deadline.strftime("%m-%d"),
        "核查人": "",
        "社区": community,
        "来源": task["source_name"],
        "姓名": task["person_name"],
        "身份证号": task["identity_number"],
        "电话号码": task["phone"],
        "地址": build_publish_address(task["original_address"], task["transfer_note"]),
        "创建时间": task["created_time"],
        "现住址": "", "核查结果": "", "研判": "", "二次反馈": "",
    }


async def _save_publish_result(
    cur,
    *,
    task_id: int,
    spreadsheet: dict[str, Any],
    business_key: str,
    request_values: dict[str, str],
    status: str,
    physical_row: int | None = None,
    verified_values: dict[str, str] | None = None,
    row_hash: str = "",
    error_code: str = "",
    error_message: str = "",
    cache_pending: bool = False,
) -> None:
    await cur.execute("""
        INSERT INTO _police_dispatch_publish_results (
            task_id, spreadsheet_id, sheet_id, physical_row,
            business_key, request_values_json, verified_values_json,
            expected_row_hash, cache_pending, status, error_code,
            error_message, attempt_count, last_attempt_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1,
                  UTC_TIMESTAMP())
        ON DUPLICATE KEY UPDATE
            spreadsheet_id=VALUES(spreadsheet_id), sheet_id=VALUES(sheet_id),
            physical_row=VALUES(physical_row), business_key=VALUES(business_key),
            request_values_json=VALUES(request_values_json),
            verified_values_json=VALUES(verified_values_json),
            expected_row_hash=VALUES(expected_row_hash),
            cache_pending=VALUES(cache_pending), status=VALUES(status),
            error_code=VALUES(error_code), error_message=VALUES(error_message),
            attempt_count=attempt_count+1, last_attempt_at=UTC_TIMESTAMP()
    """, (
        task_id, spreadsheet["id"], spreadsheet["data_sheet_id"], physical_row,
        business_key, stable_json(request_values),
        stable_json(verified_values) if verified_values is not None else None,
        row_hash, 1 if cache_pending else 0, status, error_code,
        error_message[:500],
    ))


async def _set_task_publish_state(
    cur,
    *,
    task_id: int,
    status: str,
    business_key: str,
    error: str = "",
    physical_row: int | None = None,
    linked_source_id: int | None = None,
    linked_row_hash: str = "",
    conflict_values: dict[str, str] | None = None,
    cache_pending: bool = False,
) -> None:
    task_status = "completed" if status == "success" else "publish_failed"
    if status in {"pending", "publishing", "retryable"}:
        task_status = "pending_publish"
    await cur.execute("""
        UPDATE _police_dispatch_tasks SET
            publish_status=%s, task_status=%s, publish_key=%s,
            publish_error=%s, published_row=COALESCE(%s, published_row),
            linked_source_id=%s, linked_row_hash=%s,
            conflict_values_json=%s, cache_pending=%s,
            published_at=CASE WHEN %s='success'
                THEN COALESCE(published_at, UTC_TIMESTAMP()) ELSE published_at END,
            version=version+1
        WHERE id=%s
    """, (
        status, task_status, business_key, error[:500], physical_row,
        linked_source_id, linked_row_hash,
        stable_json(conflict_values) if conflict_values is not None else None,
        1 if cache_pending else 0, status, task_id,
    ))


async def _mark_overwrite_uncertain(
    cur,
    *,
    task_id: int,
    batch_id: int,
    spreadsheet: dict[str, Any],
    physical_row: int,
    requested: dict[str, str],
    error: str,
) -> bool:
    """覆盖请求可能已到腾讯时锁定重试，交由正常同步只读对账。"""
    business_key = publish_business_key(
        requested.get("身份证号", ""),
        requested.get("电话号码", ""),
        requested.get("下发日期", ""),
    )
    await cur.execute("""
        UPDATE _police_dispatch_tasks SET
            publish_status='needs_reconciliation',
            task_status='publish_failed', publish_error=%s,
            publish_key=%s, published_row=%s,
            conflict_values_json=NULL, cache_pending=0,
            version=version+1
        WHERE id=%s AND publish_status='conflict'
    """, (error, business_key, physical_row, task_id))
    if not cur.rowcount:
        return False
    await _save_publish_result(
        cur,
        task_id=task_id,
        spreadsheet=spreadsheet,
        business_key=business_key,
        request_values=requested,
        status="needs_reconciliation",
        physical_row=physical_row,
        error_code="overwrite_uncertain",
        error_message=error,
    )
    await _refresh_batch_status(cur, batch_id)
    return True


@router.post("/batches/{batch_id}/publish")
async def publish_batch(
    batch_id: int,
    request: Request,
    user: dict = Depends(require_police_dispatch),
    conn=Depends(get_db),
):
    parser = get_parser("全链条")
    async with conn.cursor() as cur:
        batch = await _batch_payload(cur, batch_id)
        if batch["counts"]["pending_review"]:
            raise HTTPException(409, "必须先完成全批审核")
        await cur.execute("""
            SELECT duplicate_group_key,
                   SUM(final_action<>'duplicate_exclude') AS kept
            FROM _police_dispatch_tasks
            WHERE batch_id=%s AND duplicate_group_key<>''
            GROUP BY duplicate_group_key
            HAVING kept<>1
            LIMIT 1
        """, (batch_id,))
        if await cur.fetchone():
            raise HTTPException(409, "每个重复人员组必须且只能确认保留一条")
        if not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        spreadsheets = await _enabled_spreadsheets(cur, "全链条")
        if len(spreadsheets) != 1:
            raise HTTPException(409, "全链条业务没有唯一启用的腾讯来源表")
        spreadsheet = spreadsheets[0]
        await cur.execute("""
            SELECT task.id, task.source_row, task.source_name, task.person_name,
                   task.identity_number, task.phone, task.original_address,
                   task.source_created_time, task.transfer_note, community.name
            FROM _police_dispatch_tasks AS task
            JOIN _communities AS community ON community.id=task.final_community_id
            WHERE task.batch_id=%s AND task.final_action='dispatch'
              AND task.publish_status IN ('pending', 'retryable')
            ORDER BY task.source_row, task.id
        """, (batch_id,))
        pending = [
            {
                "id": int(row[0]), "source_row": int(row[1]), "source_name": str(row[2] or ""),
                "person_name": str(row[3] or ""), "identity_number": str(row[4] or ""),
                "phone": str(row[5] or ""), "original_address": str(row[6] or ""),
                "created_time": str(row[7] or ""), "transfer_note": str(row[8] or ""),
                "community": str(row[9]),
            }
            for row in await cur.fetchall()
        ]
        publish_date = await get_business_date(cur)
        await cur.execute("""
            UPDATE _police_dispatch_batches
            SET first_publish_date=COALESCE(first_publish_date, %s),
                publish_started_at=COALESCE(publish_started_at, UTC_TIMESTAMP()),
                status='publishing', last_error=''
            WHERE id=%s
        """, (publish_date, batch_id))
        await cur.execute(
            "SELECT first_publish_date FROM _police_dispatch_batches WHERE id=%s",
            (batch_id,),
        )
        publish_date = (await cur.fetchone())[0]
        if pending:
            pending_ids = [item["id"] for item in pending]
            placeholders = ",".join(["%s"] * len(pending_ids))
            await cur.execute(
                f"UPDATE _police_dispatch_tasks SET publish_status='publishing', "
                f"task_status='pending_publish', publish_error='' "
                f"WHERE id IN ({placeholders})",
                pending_ids,
            )
        if not await acquire_sheet_lock(cur, spreadsheet["id"], timeout=2):
            if pending:
                await cur.execute(
                    f"UPDATE _police_dispatch_tasks SET publish_status='retryable', "
                    f"publish_error='工作表锁正忙，尚未向腾讯发送请求' "
                    f"WHERE id IN ({placeholders})",
                    pending_ids,
                )
            await _refresh_batch_status(cur, batch_id)
            raise HTTPException(409, "全链条表格正在同步或被他人编辑，请稍后重试")

    client = None
    success_count = 0
    failed_count = 0
    try:
        async with conn.cursor() as cur:
            client = await _oauth_client(cur)
        all_rows = await client.read_all_source_rows(
            spreadsheet["file_id"], spreadsheet["data_sheet_id"],
            spreadsheet["header_row"], parser.COLUMNS,
            include_detected_headers=True,
        )
        source_rows = [row for row in all_rows if not row.get("is_header")]
        existing_by_key: dict[str, list[dict[str, Any]]] = {}
        for source in source_rows:
            key = publish_business_key(
                source["values"].get("身份证号", ""),
                source["values"].get("电话号码", ""),
                source["values"].get("下发日期", ""),
            )
            existing_by_key.setdefault(key, []).append(source)
        next_row = max([spreadsheet["header_row"], *[row["physical_row"] for row in all_rows]]) + 1
        ready: list[tuple[dict, dict, str]] = []
        async with conn.cursor() as cur:
            for task in pending:
                values = _publish_values(task, task["community"], publish_date)
                key = publish_business_key(task["identity_number"], task["phone"], values["下发日期"])
                candidates = existing_by_key.get(key, [])
                if candidates:
                    exact = next((
                        item for item in candidates
                        if _row_values_match(
                            values, item["values"], item.get("cell_meta") or {},
                            parser.COLUMNS,
                        )
                    ), None)
                    candidate = exact or candidates[0]
                    row_hash = source_row_hash({
                        column: str(candidate["values"].get(column, "") or "").strip()
                        for column in parser.COLUMNS
                    })
                    if exact:
                        success_count += 1
                        await _set_task_publish_state(
                            cur, task_id=task["id"], status="success",
                            business_key=key, physical_row=int(candidate["physical_row"]),
                            linked_row_hash=row_hash, cache_pending=True,
                        )
                        await _save_publish_result(
                            cur, task_id=task["id"], spreadsheet=spreadsheet,
                            business_key=key, request_values=values, status="success",
                            physical_row=int(candidate["physical_row"]),
                            verified_values=candidate["values"], row_hash=row_hash,
                            cache_pending=True,
                        )
                    else:
                        failed_count += 1
                        await _set_task_publish_state(
                            cur, task_id=task["id"], status="conflict",
                            business_key=key,
                            error="腾讯表格已存在相同业务主键但内容不同",
                            physical_row=int(candidate["physical_row"]),
                            linked_row_hash=row_hash,
                            conflict_values=candidate["values"],
                        )
                        await _save_publish_result(
                            cur, task_id=task["id"], spreadsheet=spreadsheet,
                            business_key=key, request_values=values, status="conflict",
                            physical_row=int(candidate["physical_row"]),
                            verified_values=candidate["values"], row_hash=row_hash,
                            error_code="content_conflict",
                            error_message="同主键内容不同",
                        )
                    continue
                existing_by_key[key] = []
                ready.append((task, values, key))

        for offset in range(0, len(ready), 50):
            chunk = ready[offset:offset + 50]
            start_row = next_row
            rows = [[values[column] for column in parser.COLUMNS] for _, values, _ in chunk]
            try:
                await client.batch_update(
                    spreadsheet["file_id"],
                    [client.build_update_range_request(
                        spreadsheet["data_sheet_id"], start_row - 1, 0, rows
                    )],
                )
                for index, (task, values, key) in enumerate(chunk):
                    physical_row = start_row + index
                    try:
                        verified = await client.read_source_row(
                            spreadsheet["file_id"], spreadsheet["data_sheet_id"],
                            physical_row, parser.COLUMNS,
                        )
                        matched = _row_values_match(
                            values, verified["values"], verified.get("cell_meta") or {},
                            parser.COLUMNS,
                        )
                    except Exception:
                        verified = None
                        matched = False
                    async with conn.cursor() as cur:
                        if matched and verified is not None:
                            verified_values = {
                                column: str(verified["values"].get(column, "") or "").strip()
                                for column in parser.COLUMNS
                            }
                            verified_hash = source_row_hash(verified_values)
                            await _set_task_publish_state(
                                cur, task_id=task["id"], status="success",
                                business_key=key, physical_row=physical_row,
                                linked_row_hash=verified_hash, cache_pending=True,
                            )
                            await _save_publish_result(
                                cur, task_id=task["id"], spreadsheet=spreadsheet,
                                business_key=key, request_values=values,
                                status="success", physical_row=physical_row,
                                verified_values=verified_values,
                                row_hash=verified_hash, cache_pending=True,
                            )
                            success_count += 1
                        else:
                            error = "腾讯已收到写入请求，但回读结果尚未确认"
                            await _set_task_publish_state(
                                cur, task_id=task["id"],
                                status="needs_reconciliation",
                                business_key=key, error=error,
                                physical_row=physical_row,
                            )
                            await _save_publish_result(
                                cur, task_id=task["id"], spreadsheet=spreadsheet,
                                business_key=key, request_values=values,
                                status="needs_reconciliation",
                                physical_row=physical_row,
                                verified_values=(verified or {}).get("values"),
                                error_code="verification_uncertain",
                                error_message=error,
                            )
                            failed_count += 1
                next_row += len(chunk)
            except Exception as exc:
                failed_count += len(chunk)
                safe_error = "腾讯写入请求结果不确定，等待下次正常同步对账"
                if isinstance(exc, HTTPException):
                    safe_error = str(exc.detail)[:500]
                elif isinstance(exc, TxDocsAPIError):
                    safe_error = str(exc)[:500]
                async with conn.cursor() as cur:
                    for task, values, key in chunk:
                        await _set_task_publish_state(
                            cur, task_id=task["id"],
                            status="needs_reconciliation",
                            business_key=key, error=safe_error,
                        )
                        await _save_publish_result(
                            cur, task_id=task["id"], spreadsheet=spreadsheet,
                            business_key=key, request_values=values,
                            status="needs_reconciliation",
                            error_code="request_uncertain",
                            error_message=safe_error,
                        )
                break
        try:
            await _refresh_spreadsheet(conn, client, spreadsheet)
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE _police_dispatch_tasks AS task
                    JOIN _police_dispatch_publish_results AS result
                      ON result.task_id=task.id
                    JOIN _online_source_rows AS source
                      ON source.spreadsheet_id=result.spreadsheet_id
                     AND source.sheet_id=result.sheet_id
                     AND source.physical_row=result.physical_row
                    SET task.linked_source_id=source.id,
                        task.linked_row_hash=source.row_hash,
                        result.source_row_id=source.id,
                        result.expected_row_hash=source.row_hash
                    WHERE task.batch_id=%s
                """, (batch_id,))
                await cur.execute("""
                    UPDATE _police_dispatch_tasks AS task
                    JOIN _police_dispatch_publish_results AS result
                      ON result.task_id=task.id
                    SET task.cache_pending=0, result.cache_pending=0
                    WHERE task.batch_id=%s AND task.publish_status='success'
                """, (batch_id,))
        except Exception:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE _police_dispatch_tasks AS task
                    JOIN _police_dispatch_publish_results AS result
                      ON result.task_id=task.id
                    SET task.cache_pending=1, result.cache_pending=1
                    WHERE task.batch_id=%s AND task.publish_status='success'
                """, (batch_id,))
    finally:
        try:
            if client:
                await client.close()
        finally:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE _police_dispatch_tasks
                    SET publish_status='retryable', task_status='pending_publish',
                        publish_error='尚未向腾讯发送，可安全重试'
                    WHERE batch_id=%s AND publish_status='publishing'
                """, (batch_id,))
                counts = await _refresh_batch_status(cur, batch_id)
                await cur.execute("""
                    UPDATE _police_dispatch_batches SET
                        last_error=CASE
                            WHEN %s>0 THEN '部分任务等待同步对账或存在内容冲突'
                            ELSE '' END
                    WHERE id=%s
                """, (failed_count, batch_id))
                await release_sheet_lock(cur, spreadsheet["id"])
    await record_admin_audit(
        user, "police_dispatch.publish", target_type="police_dispatch_batch",
        target_name=str(batch_id), detail={"success": success_count, "failed": failed_count},
        result="partial" if failed_count else "success", **request_audit_fields(request),
    )
    return {
        "message": "发布完成" if not failed_count else "部分任务需要等待同步对账或人工处理冲突",
        "success_count": success_count, "failed_count": failed_count,
        "counts": counts,
    }
