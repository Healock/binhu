"""Read-only source acquisition and explicit confirmation for visit data."""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import get_db
from deps import require_permission
from services.audit import record_admin_audit, request_audit_fields
from services.business_time import get_business_date, get_business_timezone_name
from services.permissions import VISIT_SOURCE_MANAGE
from services.visit_import import (
    VISIT_IMPORT_LOCK_NAME,
    VisitWorkbookError,
)
from services.visit_source import VisitSourceError, commit_rows, fetch_rows, preview_diff, safe_payload

router = APIRouter(prefix="/api/visits/sources", tags=["走访来源获取"])


class SourcePreviewRequest(BaseModel):
    source: Literal["detail", "rating", "both"] = "both"
    start_date: date
    end_date: date


class SourceConfirmRequest(BaseModel):
    run_ids: list[int] = Field(min_length=1, max_length=2)
    strategy: Literal["replace", "keep"] = "replace"


def _json_value(value):
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


def _run_view(row: tuple) -> dict:
    return {
        "id": int(row[0]),
        "source": row[1],
        "trigger_source": row[2],
        "status": row[3],
        "requested_by": row[4],
        "start_date": row[5].isoformat() if row[5] else None,
        "end_date": row[6].isoformat() if row[6] else None,
        "response_business_date": row[7].isoformat() if row[7] else None,
        "source_page": row[8],
        "record_count": int(row[9] or 0),
        "valid_count": int(row[10] or 0),
        "issue_count": int(row[11] or 0),
        "summary": _json_value(row[12]),
        "error_code": row[13],
        "error_message": row[14],
        "confirmed_by": row[15],
        "confirmed_at": row[16].isoformat() if row[16] else None,
        "created_at": row[17].isoformat() if row[17] else None,
    }


async def _lock(conn, acquire: bool) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT GET_LOCK(%s, 0)" if acquire else "SELECT RELEASE_LOCK(%s)",
            (VISIT_IMPORT_LOCK_NAME,),
        )
        row = await cur.fetchone()
    return bool(acquire and row and row[0] == 1)


async def _insert_run(conn, user_id: int, source: str, start_date: date, end_date: date, result: dict) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO _visit_source_runs (
                source_kind, trigger_source, status, requested_by,
                requested_start_date, requested_end_date, response_business_date,
                source_page, source_url, record_count, valid_count, issue_count,
                summary_json, payload_json, error_code, error_message
            ) VALUES (%s, 'manual', %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source,
                result.get("status", "failed"),
                user_id,
                start_date,
                end_date,
                result.get("response_business_date"),
                result.get("source_page", source),
                result.get("record_count", 0),
                result.get("valid_count", 0),
                result.get("issue_count", 0),
                json.dumps({"issues": result.get("issues", []), "diff": result.get("diff", {})}, ensure_ascii=False),
                safe_payload(result.get("rows", [])) if result.get("rows") else None,
                result.get("error_code"),
                result.get("error_message"),
            ),
        )
        return int(cur.lastrowid)


@router.post("/preview")
async def preview_source(
    payload: SourcePreviewRequest,
    request: Request,
    user: dict = Depends(require_permission(VISIT_SOURCE_MANAGE)),
    conn=Depends(get_db),
):
    if payload.start_date > payload.end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    sources = ["detail", "rating"] if payload.source == "both" else [payload.source]
    async with conn.cursor() as cur:
        timezone_name = await get_business_timezone_name(cur)
    data = []
    for source in sources:
        try:
            result = await fetch_rows(source, payload.start_date, payload.end_date)
            result["status"] = "pending_confirmation"
            result["source_page"] = "走访明细" if source == "detail" else "新星级评分管理"
            result["diff"] = await preview_diff(
                conn,
                kind=source,
                rows=result["rows"],
                timezone_name=timezone_name,
            )
        except (VisitSourceError, VisitWorkbookError) as exc:
            error_code = exc.code if isinstance(exc, VisitSourceError) else "schema_changed"
            result = {
                "status": "failed",
                "source_page": "走访明细" if source == "detail" else "新星级评分管理",
                "error_code": error_code,
                "error_message": str(exc),
                "record_count": 0,
                "valid_count": 0,
                "issue_count": 1,
                "issues": [str(exc)],
                "rows": [],
            }
        run_id = await _insert_run(conn, int(user["id"]), source, payload.start_date, payload.end_date, result)
        data.append({
            "id": run_id,
            "source": source,
            "status": result["status"],
            "source_page": result["source_page"],
            "record_count": result.get("record_count", 0),
            "valid_count": result.get("valid_count", 0),
            "issue_count": result.get("issue_count", 0),
            "response_business_date": result.get("response_business_date"),
            "issues": result.get("issues", []),
            "diff": result.get("diff", {}),
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message"),
        })
    await record_admin_audit(
        user,
        "visit_source.preview",
        target_type="visit_source",
        target_name=payload.source,
        result="success" if all(item["status"] != "failed" for item in data) else "failed",
        detail={"run_ids": [item["id"] for item in data]},
        **request_audit_fields(request),
    )
    return {"data": data, "requires_confirmation": any(item["status"] == "pending_confirmation" for item in data)}


@router.post("/confirm")
async def confirm_source(
    payload: SourceConfirmRequest,
    request: Request,
    user: dict = Depends(require_permission(VISIT_SOURCE_MANAGE)),
    conn=Depends(get_db),
):
    if not await _lock(conn, True):
        raise HTTPException(status_code=409, detail="当前已有走访或星级数据正在处理")
    try:
        placeholders = ",".join(["%s"] * len(payload.run_ids))
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT id, source_kind, status, requested_start_date, requested_end_date, payload_json FROM _visit_source_runs WHERE id IN ({placeholders})",
                tuple(payload.run_ids),
            )
            rows = {int(row[0]): row for row in await cur.fetchall()}
        results = []
        for run_id in payload.run_ids:
            row = rows.get(run_id)
            if not row:
                raise HTTPException(status_code=404, detail=f"来源运行 {run_id} 不存在")
            if row[2] != "pending_confirmation":
                raise HTTPException(status_code=409, detail=f"来源运行 {run_id} 不在待确认状态")
            if payload.strategy == "keep":
                async with conn.cursor() as cur:
                    await cur.execute("UPDATE _visit_source_runs SET status='kept', confirmed_by=%s, confirmed_at=UTC_TIMESTAMP() WHERE id=%s", (user["id"], run_id))
                results.append({"id": run_id, "status": "kept"})
                continue
            values = _json_value(row[5])
            if not isinstance(values, list) or not values:
                raise HTTPException(status_code=409, detail=f"来源运行 {run_id} 缺少可导入数据")
            result = await commit_rows(
                conn,
                kind=row[1],
                rows=values,
                start_date=row[3],
                user_id=int(user["id"]),
                source_type="manual_source",
                source_run_id=run_id,
            )
            batch_id = int(result["batch_id"])
            status = "confirmed" if result.get("status") in ("success", "partial") else "failed"
            async with conn.cursor() as cur:
                if status == "confirmed":
                    await cur.execute(
                        """
                        UPDATE _visit_source_runs
                        SET status='superseded', superseded_by=%s
                        WHERE source_kind=%s
                          AND requested_start_date=%s
                          AND requested_end_date=%s
                          AND status='confirmed'
                          AND id<>%s
                        """,
                        (run_id, row[1], row[3], row[4], run_id),
                    )
                await cur.execute(
                    "UPDATE _visit_source_runs SET status=%s, confirmed_by=%s, confirmed_at=UTC_TIMESTAMP(), summary_json=%s WHERE id=%s",
                    (status, user["id"], json.dumps({"batch_id": batch_id, "import_status": result.get("status")}, ensure_ascii=False), run_id),
                )
            results.append({"id": run_id, "status": status, "batch_id": batch_id, "import_status": result.get("status")})
        await record_admin_audit(user, "visit_source.confirm", target_type="visit_source", target_name=",".join(map(str, payload.run_ids)), result="success", detail={"strategy": payload.strategy, "run_ids": payload.run_ids}, **request_audit_fields(request))
        return {"data": results, "strategy": payload.strategy}
    finally:
        await _lock(conn, False)


@router.get("/status")
async def source_status(
    user: dict = Depends(require_permission(VISIT_SOURCE_MANAGE)),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        timezone_name = await get_business_timezone_name(cur)
        business_date = await get_business_date(cur)
        await cur.execute("SELECT id, source_kind, trigger_source, status, requested_by, requested_start_date, requested_end_date, response_business_date, source_page, record_count, valid_count, issue_count, summary_json, error_code, error_message, confirmed_by, confirmed_at, created_at FROM _visit_source_runs ORDER BY id DESC LIMIT 50")
        rows = await cur.fetchall()
        await cur.execute(
            """
            SELECT id, import_type, source_type, source_run_id, finished_at
            FROM _visit_import_batches
            WHERE status IN ('success', 'partial')
            ORDER BY id DESC
            LIMIT 50
            """
        )
        batch_rows = await cur.fetchall()
    latest_attempts = {}
    active = {}
    for row in rows:
        latest_attempts.setdefault(row[1], _run_view(row))
        if row[3] == "confirmed":
            active.setdefault(row[1], _run_view(row))
    current_sources = {}
    for batch in batch_rows:
        current_sources.setdefault(
            str(batch[1]),
            {
                "batch_id": int(batch[0]),
                "source_type": str(batch[2] or "manual"),
                "source_run_id": int(batch[3]) if batch[3] is not None else None,
                "finished_at": batch[4].isoformat() if batch[4] else None,
            },
        )
    return {
        "business_date": business_date.isoformat(),
        "timezone": timezone_name,
        "data": active,
        "current_sources": current_sources,
        "latest_attempts": latest_attempts,
        "runs": [_run_view(row) for row in rows[:20]],
    }


@router.get("/runs")
async def source_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_permission(VISIT_SOURCE_MANAGE)),
    conn=Depends(get_db),
):
    del user
    offset = (page - 1) * page_size
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM _visit_source_runs")
        total = int((await cur.fetchone())[0] or 0)
        await cur.execute("SELECT id, source_kind, trigger_source, status, requested_by, requested_start_date, requested_end_date, response_business_date, source_page, record_count, valid_count, issue_count, summary_json, error_code, error_message, confirmed_by, confirmed_at, created_at FROM _visit_source_runs ORDER BY id DESC LIMIT %s OFFSET %s", (page_size, offset))
        rows = await cur.fetchall()
    return {"data": [_run_view(row) for row in rows], "total": total, "page": page, "page_size": page_size}
