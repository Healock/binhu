"""全链条公安网原始数据、反馈导出与归档管理。"""

from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import settings
from database import get_db
from routers.police_dispatch import require_fullchain_archive
from services.audit import record_admin_audit, request_audit_fields
from services.business_time import get_business_date
from services.fullchain_archive import (
    MAX_POLICE_FILE_BYTES,
    archive_dir,
    build_archive_workbook,
    parse_police_raw,
)
from services.fullchain_archive_jobs import get_archive_export, launch_fullchain_archive_export
from services.police_dispatch import stable_json


router = APIRouter(prefix="/api/police-dispatch/fullchain-archive", tags=["全链条反馈归档"])
ARCHIVE_RESULTS = {"离苏", "无需登记", "移交（所外）"}
REVIEW_RESULTS = {"移交", "移交（所内）"}


class CandidateSearch(BaseModel):
    stages: list[Literal["direct", "review", "registered"]] = Field(default_factory=lambda: ["direct", "review", "registered"])
    keyword: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


class ReviewDecision(BaseModel):
    row_key: str = Field(min_length=32, max_length=32)
    decision: Literal["transfer_internal", "transfer_external", "keep", "archive"]
    note: str = Field(default="", max_length=500)


class ExportSelection(BaseModel):
    source_ids: list[int] = Field(min_length=1, max_length=5000)
    preview_token: str = Field(min_length=64, max_length=64)


def _preview_token(items: list[dict[str, Any]]) -> str:
    payload = stable_json([
        [item["source_id"], item["revision"], item["row_hash"], item["category"]]
        for item in sorted(items, key=lambda value: value["source_id"])
    ])
    return hmac.new(settings.registry_hmac_key.encode(), payload.encode(), sha256).hexdigest()


def _parse_deadline(value: str, today: date) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in (r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", r"^(\d{1,2})[-/.](\d{1,2})$"):
        match = re.match(pattern, text)
        if not match:
            continue
        try:
            if len(match.groups()) == 3:
                return date(*map(int, match.groups()))
            month, day = map(int, match.groups())
            candidate = date(today.year, month, day)
            if candidate > today and (candidate - today).days > 180:
                candidate = candidate.replace(year=today.year - 1)
            return candidate
        except ValueError:
            return None
    return None


async def _latest_raw_upload(cur) -> int | None:
    await cur.execute("SELECT id FROM _fullchain_police_raw_uploads WHERE status='confirmed' ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    return int(row[0]) if row else None


async def _candidate_rows(cur, source_ids: list[int] | None = None) -> list[dict[str, Any]]:
    today = await get_business_date(cur)
    latest_upload = await _latest_raw_upload(cur)
    params: list[Any] = []
    where = ""
    if source_ids:
        placeholders = ",".join(["%s"] * len(source_ids))
        where = f" AND source.id IN ({placeholders})"
        params.extend(source_ids)
    await cur.execute(f"""
        SELECT source.id,source.row_key,source.revision,source.row_hash,
               source.physical_row,source.spreadsheet_id,source.sheet_id,
               source.values_json,projection.identity_hmac,
               projection.source_count,projection.conflict,
               review.decision,review.note
        FROM _online_source_rows source
        JOIN _online_source_projection projection
          ON projection.parser_type=source.parser_type AND projection.row_key=source.row_key
        LEFT JOIN _fullchain_archive_reviews review
          ON review.parser_type=source.parser_type AND review.row_key=source.row_key
        WHERE source.parser_type='全链条'{where}
        ORDER BY source.id
    """, params)
    rows: list[dict[str, Any]] = []
    for raw in await cur.fetchall():
        values = json.loads(raw[7] or "{}")
        result = str(values.get("核查结果") or "").strip()
        source_count = int(raw[9] or 0)
        projection_conflict = bool(raw[10])
        decision = str(raw[11] or "")
        category = stage = reason = ""
        eligible = False
        if result in ARCHIVE_RESULTS:
            category, stage, reason, eligible = result, "direct", "核查结果可直接反馈归档", True
        elif result == "移交":
            stage, reason = "review", "历史移交结果未区分所内或所外"
            if decision == "transfer_external":
                category, eligible = "移交（所外）", True
        elif result == "移交（所内）":
            stage, reason = "review", "所内移交需基础管控审核"
            if decision == "archive":
                category, eligible = "移交（所内）", True
        elif result == "无法核实":
            deadline = _parse_deadline(values.get("截止日期", ""), today)
            if deadline and deadline < today:
                stage, reason = "review", f"无法核实且已超过截止日期 {deadline.isoformat()}"
                if decision == "archive":
                    category, eligible = "无法核实", True
        elif result == "已登记":
            stage, reason = "registered", "需与最近一次公安网原始数据比对"
            if latest_upload and raw[8]:
                await cur.execute(
                    "SELECT 1 FROM _fullchain_police_raw_identities WHERE upload_id=%s AND identity_hmac=%s LIMIT 1",
                    (latest_upload, raw[8]),
                )
                if not await cur.fetchone():
                    category, eligible, reason = "已登记", True, "最近公安网原始数据已不再包含该人员"
                else:
                    reason = "最近公安网原始数据仍包含该人员，继续保留"
            elif not latest_upload:
                reason = "尚未确认公安网原始数据，不能判断是否已登记"
        if stage and (source_count != 1 or projection_conflict):
            eligible = False
            conflict_reason = "存在重复或冲突来源行，需先完成来源核查"
            reason = f"{reason}；{conflict_reason}" if reason else conflict_reason
        if stage:
            rows.append({
                "source_id": int(raw[0]), "row_key": str(raw[1]), "revision": int(raw[2]),
                "row_hash": str(raw[3]), "physical_row": int(raw[4]),
                "spreadsheet_id": int(raw[5]), "sheet_id": str(raw[6]),
                "name": str(values.get("姓名") or ""), "identity": str(values.get("身份证号") or ""),
                "phone": str(values.get("电话号码") or ""), "address": str(values.get("现住址") or values.get("地址") or ""),
                "source": str(values.get("来源") or ""), "registration": str(values.get("登记情况") or ""),
                "result": result, "deadline": str(values.get("截止日期") or ""),
                "stage": stage, "category": category, "eligible": eligible,
                "reason": reason, "decision": decision, "review_note": str(raw[12] or ""),
                "source_count": source_count, "conflict": projection_conflict,
            })
    return rows


@router.post("/police-raw/preview")
async def preview_police_raw(file: UploadFile = File(...), user: dict = Depends(require_fullchain_archive)):
    del user
    content = await file.read(MAX_POLICE_FILE_BYTES + 1)
    if len(content) > MAX_POLICE_FILE_BYTES:
        raise HTTPException(413, "公安网原始文件不能超过 30MB")
    try:
        result = parse_police_raw(content, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    token_payload = f"{result['file_sha256']}:{result['row_count']}:{result['sheet_name']}"
    result["preview_token"] = hmac.new(settings.registry_hmac_key.encode(), token_payload.encode(), sha256).hexdigest()
    result.pop("identity_hmacs", None)
    return result


@router.post("/police-raw/confirm")
async def confirm_police_raw(
    request: Request, file: UploadFile = File(...), preview_token: str = Form(...),
    user: dict = Depends(require_fullchain_archive), conn=Depends(get_db),
):
    content = await file.read(MAX_POLICE_FILE_BYTES + 1)
    if len(content) > MAX_POLICE_FILE_BYTES:
        raise HTTPException(413, "公安网原始文件不能超过 30MB")
    try:
        parsed = parse_police_raw(content, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = f"{parsed['file_sha256']}:{parsed['row_count']}:{parsed['sheet_name']}"
    expected = hmac.new(settings.registry_hmac_key.encode(), payload.encode(), sha256).hexdigest()
    if not hmac.compare_digest(expected, preview_token):
        raise HTTPException(409, "预览已经变化，请重新预览")
    archive_path = archive_dir() / f"raw-{parsed['file_sha256']}.bin"
    staged_path: str | None = None
    created_final = False
    if not archive_path.is_file():
        with tempfile.NamedTemporaryFile(
            dir=archive_dir(), prefix=f".raw-{parsed['file_sha256']}.", suffix=".tmp", delete=False
        ) as staged:
            staged.write(content)
            staged_path = staged.name
        os.replace(staged_path, archive_path)
        staged_path = None
        created_final = True
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE _fullchain_police_raw_uploads SET status='superseded' WHERE status='confirmed'")
            await cur.execute("""
                INSERT INTO _fullchain_police_raw_uploads
                (file_name,file_sha256,sheet_name,row_count,invalid_count,duplicate_count,storage_key,status,uploaded_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'confirmed',%s)
                ON DUPLICATE KEY UPDATE status='confirmed',storage_key=VALUES(storage_key),uploaded_by=VALUES(uploaded_by)
            """, (parsed["filename"], parsed["file_sha256"], parsed["sheet_name"], parsed["row_count"], parsed["invalid_count"], parsed["duplicate_count"], f"raw-{parsed['file_sha256']}.bin", user["id"]))
            upload_id = int(cur.lastrowid or 0)
            if not upload_id:
                await cur.execute("SELECT id FROM _fullchain_police_raw_uploads WHERE file_sha256=%s", (parsed["file_sha256"],))
                upload_id = int((await cur.fetchone())[0])
            await cur.execute("DELETE FROM _fullchain_police_raw_identities WHERE upload_id=%s", (upload_id,))
            await cur.executemany(
                "INSERT INTO _fullchain_police_raw_identities (upload_id,identity_hmac) VALUES (%s,%s)",
                [(upload_id, digest) for digest in parsed["identity_hmacs"]],
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        if staged_path:
            try:
                os.unlink(staged_path)
            except FileNotFoundError:
                pass
        if created_final:
            try:
                archive_path.unlink()
            except FileNotFoundError:
                pass
        raise
    await record_admin_audit(user, "fullchain.police_raw.confirm", target_type="fullchain_police_raw_upload", target_name=str(upload_id), detail={"row_count": parsed["row_count"], "file_sha256": parsed["file_sha256"]}, **request_audit_fields(request))
    return {"id": upload_id, "message": "公安网原始数据已确认，可用于已登记比对", "row_count": parsed["row_count"]}


@router.get("/police-raw/uploads")
async def list_police_raw_uploads(user: dict = Depends(require_fullchain_archive), conn=Depends(get_db)):
    del user
    async with conn.cursor() as cur:
        await cur.execute("SELECT id,file_name,row_count,invalid_count,duplicate_count,status,storage_key,created_at FROM _fullchain_police_raw_uploads ORDER BY id DESC LIMIT 50")
        rows = await cur.fetchall()
    return {"data": [{"id": int(r[0]), "file_name": str(r[1]), "row_count": int(r[2]), "invalid_count": int(r[3]), "duplicate_count": int(r[4]), "status": str(r[5]), "storage_key": str(r[6]), "created_at": r[7].isoformat() + "Z"} for r in rows]}


@router.get("/police-raw/uploads/{upload_id}/download")
async def download_police_raw(upload_id: int, user: dict = Depends(require_fullchain_archive), conn=Depends(get_db)):
    del user
    async with conn.cursor() as cur:
        await cur.execute("SELECT file_name,storage_key FROM _fullchain_police_raw_uploads WHERE id=%s", (upload_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "公安网原始数据记录不存在")
    path = archive_dir() / str(row[1])
    if not path.is_file():
        raise HTTPException(410, "原始文件已丢失，请联系超级管理员")
    return FileResponse(path, media_type="application/octet-stream", filename=str(row[0]))


@router.post("/candidates/search")
async def search_candidates(data: CandidateSearch, user: dict = Depends(require_fullchain_archive), conn=Depends(get_db)):
    del user
    async with conn.cursor() as cur:
        rows = await _candidate_rows(cur)
    keyword = data.keyword.strip().lower()
    rows = [row for row in rows if row["stage"] in data.stages and (not keyword or keyword in "\n".join([row["name"], row["identity"], row["phone"], row["address"]]).lower())]
    start = (data.page - 1) * data.page_size
    return {"data": rows[start:start + data.page_size], "total": len(rows), "page": data.page, "page_size": data.page_size, "counts": {stage: sum(row["stage"] == stage for row in rows) for stage in ("direct", "review", "registered")}}


@router.post("/reviews")
async def save_review(data: ReviewDecision, request: Request, user: dict = Depends(require_fullchain_archive), conn=Depends(get_db)):
    async with conn.cursor() as cur:
        await cur.execute("SELECT values_json FROM _online_source_projection WHERE parser_type='全链条' AND row_key=%s", (data.row_key,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(409, "任务已经变化，请刷新")
        result = str(json.loads(row[0] or "{}").get("核查结果") or "").strip()
        allowed = {"移交": {"transfer_internal", "transfer_external"}, "移交（所内）": {"keep", "archive"}, "无法核实": {"keep", "archive"}}
        if data.decision not in allowed.get(result, set()):
            raise HTTPException(409, "当前核查结果不允许使用该审核决定")
        await cur.execute("""
            INSERT INTO _fullchain_archive_reviews (parser_type,row_key,decision,note,decided_by)
            VALUES ('全链条',%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE decision=VALUES(decision),note=VALUES(note),decided_by=VALUES(decided_by),decided_at=UTC_TIMESTAMP()
        """, (data.row_key, data.decision, data.note.strip(), user["id"]))
    await record_admin_audit(user, "fullchain.archive.review", target_type="online_task", target_name=f"全链条:{data.row_key}", detail={"decision": data.decision}, **request_audit_fields(request))
    return {"message": "审核决定已保存"}


@router.post("/exports/preview")
async def preview_export(source_ids: list[int], user: dict = Depends(require_fullchain_archive), conn=Depends(get_db)):
    del user
    ids = list(dict.fromkeys(source_ids))
    if not ids or len(ids) > settings.FULLCHAIN_ARCHIVE_MAX_ROWS:
        raise HTTPException(400, "请选择 1 至 5000 条数据")
    async with conn.cursor() as cur:
        rows = await _candidate_rows(cur, ids)
    eligible = [row for row in rows if row["eligible"]]
    eligible_ids = {row["source_id"] for row in eligible}
    if eligible_ids != set(ids):
        raise HTTPException(409, "部分数据尚不满足归档条件，请刷新后重新选择")
    return {"total": len(eligible), "categories": {category: sum(row["category"] == category for row in eligible) for category in sorted({row["category"] for row in eligible})}, "rows": [{key: row[key] for key in ("source_id", "name", "result", "category", "reason")} for row in eligible[:100]], "preview_token": _preview_token(eligible)}


@router.post("/exports", status_code=202)
async def create_export(data: ExportSelection, request: Request, user: dict = Depends(require_fullchain_archive), conn=Depends(get_db)):
    ids = list(dict.fromkeys(data.source_ids))
    async with conn.cursor() as cur:
        rows = await _candidate_rows(cur, ids)
    eligible = [row for row in rows if row["eligible"]]
    eligible_ids = {row["source_id"] for row in eligible}
    if eligible_ids != set(ids) or not hmac.compare_digest(_preview_token(eligible), data.preview_token):
        raise HTTPException(409, "候选数据已经变化，请重新预览")
    categories = {category: sum(row["category"] == category for row in eligible) for category in sorted({row["category"] for row in eligible})}
    export_no = datetime.utcnow().strftime("FCA-%Y%m%d-%H%M%S-%f")[:32]
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO _fullchain_archive_exports
                (export_no,status,phase,file_name,storage_key,total_count,categories_json,requested_by)
                VALUES (%s,'queued','preparing','', '', %s,%s,%s)
            """, (export_no, len(eligible), stable_json(categories), user["id"]))
            export_id = int(cur.lastrowid)
            filename = f"全链条反馈归档-{export_no}.xlsx"
            storage_key = f"export-{export_id}.xlsx"
            content = build_archive_workbook(eligible, export_id, datetime.now())
            path = archive_dir() / storage_key
            staged_path: str | None = None
            created_final = False
            if not path.is_file():
                with tempfile.NamedTemporaryFile(
                    dir=archive_dir(), prefix=f".export-{export_id}.", suffix=".tmp", delete=False
                ) as staged:
                    staged.write(content)
                    staged_path = staged.name
                os.replace(staged_path, path)
                staged_path = None
                created_final = True
            digest = sha256(content).hexdigest()
            await cur.execute("UPDATE _fullchain_archive_exports SET file_name=%s,storage_key=%s,file_sha256=%s,phase='queued' WHERE id=%s", (filename, storage_key, digest, export_id))
            await cur.executemany("""
                INSERT INTO _fullchain_archive_export_items
                (export_id,parser_type,row_key,source_id,spreadsheet_id,sheet_id,physical_row,expected_revision,expected_row_hash,category)
                VALUES (%s,'全链条',%s,%s,%s,%s,%s,%s,%s,%s)
            """, [(export_id, row["row_key"], row["source_id"], row["spreadsheet_id"], row["sheet_id"], row["physical_row"], row["revision"], row["row_hash"], row["category"]) for row in eligible])
        await conn.commit()
    except Exception:
        await conn.rollback()
        if 'staged_path' in locals() and staged_path:
            try:
                os.unlink(staged_path)
            except FileNotFoundError:
                pass
        if 'created_final' in locals() and created_final:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    launch_fullchain_archive_export(export_id)
    await record_admin_audit(user, "fullchain.archive.export", target_type="fullchain_archive_export", target_name=str(export_id), detail={"row_count": len(eligible), "categories": categories}, **request_audit_fields(request))
    return {"message": "反馈文件已生成，后台开始归档", "export": await get_archive_export(export_id)}


@router.get("/exports")
async def list_exports(user: dict = Depends(require_fullchain_archive), conn=Depends(get_db)):
    del user
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM _fullchain_archive_exports ORDER BY id DESC LIMIT 100")
        ids = [int(row[0]) for row in await cur.fetchall()]
    return {"data": [item for export_id in ids if (item := await get_archive_export(export_id))]}


@router.get("/exports/{export_id}")
async def export_detail(export_id: int, user: dict = Depends(require_fullchain_archive)):
    del user
    data = await get_archive_export(export_id)
    if not data:
        raise HTTPException(404, "归档记录不存在")
    return data


@router.get("/exports/{export_id}/download")
async def download_export(export_id: int, user: dict = Depends(require_fullchain_archive)):
    del user
    data = await get_archive_export(export_id)
    if not data:
        raise HTTPException(404, "归档记录不存在")
    path = archive_dir() / data["storage_key"]
    if not path.is_file():
        raise HTTPException(410, "归档文件已丢失，请联系超级管理员")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=data["file_name"])
