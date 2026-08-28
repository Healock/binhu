"""全链条反馈导出、归档管理和历史公安网材料只读访问。"""

from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import settings
from database import get_db
from routers.police_dispatch import require_fullchain_archive
from services.audit import record_admin_audit, request_audit_fields
from services.fullchain_archive import (
    REGISTRATION_ARCHIVE_RULE_VERSION,
    archive_dir,
    build_archive_workbook,
    registration_archive_available_at,
    registration_archive_ready,
)
from services.fullchain_archive_jobs import get_archive_export, launch_fullchain_archive_export
from services.police_dispatch import stable_json
from services.task_workflow import TASK_WORKFLOWS
from services.unverifiable_review import (
    FINAL_UNVERIFIABLE,
    review_events_by_flow_ids,
    review_export_fields,
    review_flows_by_rows,
    supports_unverifiable_review,
)
from services.local_source import local_data_source_enabled


router = APIRouter(prefix="/api/police-dispatch/fullchain-archive", tags=["全链条反馈归档"])
ARCHIVE_RESULTS = {"离苏", "无需登记", "移交（所外）"}
REVIEW_RESULTS = {"移交", "移交（所内）"}
POLICE_RAW_RETIRED_MESSAGE = "公安网原始数据比对功能已停用，已登记归档改由居住证自动确认。"


class CandidateSearch(BaseModel):
    parser_type: str = "全链条"
    stages: list[Literal["direct", "review", "registered"]] = Field(default_factory=lambda: ["direct", "review", "registered"])
    keyword: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


class ReviewDecision(BaseModel):
    parser_type: str = "全链条"
    row_key: str = Field(min_length=32, max_length=32)
    decision: Literal["transfer_internal", "transfer_external", "keep", "archive"]
    note: str = Field(default="", max_length=500)


class ExportSelection(BaseModel):
    source_ids: list[int] = Field(min_length=1, max_length=5000)
    preview_token: str = Field(min_length=64, max_length=64)


def _preview_token(items: list[dict[str, Any]]) -> str:
    payload = stable_json([
        [
            item["source_id"], item["revision"], item["row_hash"], item["category"],
            item.get("registration_status", ""), item.get("registration_confirmed_at", ""),
            item.get("registration_identity_hmac", ""), item.get("registration_property_id"),
            item.get("registration_property_version"), item.get("candidate_rule_version", ""),
        ]
        for item in sorted(items, key=lambda value: value["source_id"])
    ])
    return hmac.new(settings.registry_hmac_key.encode(), payload.encode(), sha256).hexdigest()


def _parse_deadline(value: str, today: date) -> date | None:
    """兼容旧全链条日期文本；新无法核实流程不再依赖它决定归档资格。"""
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


async def _candidate_rows(
    cur,
    parser_type: str = "全链条",
    source_ids: list[int] | None = None,
    *,
    include_source_values: bool = False,
) -> list[dict[str, Any]]:
    if parser_type not in TASK_WORKFLOWS or not supports_unverifiable_review(parser_type):
        raise HTTPException(400, "该业务不支持无法核实导出")
    params: list[Any] = []
    where = ""
    if source_ids:
        placeholders = ",".join(["%s"] * len(source_ids))
        where = f" AND source.id IN ({placeholders})"
        params.extend(source_ids)
    registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
    await cur.execute(f"""
        SELECT source.id,source.row_key,source.revision,source.row_hash,
               source.physical_row,source.spreadsheet_id,source.sheet_id,
               source.values_json,projection.identity_hmac,
               projection.source_count,projection.conflict,
               review.decision,review.note,
               registration.status,registration.confirmed_at,
               registration.source_id,registration.source_revision,
               registration.source_row_hash,registration.identity_hmac,
               registration.task_community,registration.property_id,
               registration.property_version,property.status,
               property.current_version,
               EXISTS (
                   SELECT 1 FROM _online_local_changes local_change
                   WHERE local_change.source_id=source.id
                     AND local_change.status IN ('pending','processing','retry','conflict')
               ) AS has_active_writeback,
               EXISTS (
                   SELECT 1
                   FROM _fullchain_archive_export_items other_item
                   JOIN _fullchain_archive_exports other_export
                     ON other_export.id=other_item.export_id
                   WHERE other_item.source_id=source.id
                     AND (
                         other_export.status IN ('queued','running')
                         OR other_item.external_delete_state IN ('deleting','deleted')
                     )
               ) AS has_active_archive,
               projection.community
        FROM _online_source_rows source
        JOIN _online_source_projection projection
          ON projection.parser_type=source.parser_type AND projection.row_key=source.row_key
        LEFT JOIN _fullchain_archive_reviews review
          ON review.parser_type=source.parser_type AND review.row_key=source.row_key
        LEFT JOIN _task_registration_links registration
          ON registration.parser_type=source.parser_type
         AND registration.row_key=source.row_key
        LEFT JOIN `{registry}`.registry_properties property
          ON property.id=registration.property_id
        WHERE source.parser_type=%s AND source.archived_at IS NULL{where}
        ORDER BY source.id
    """, [parser_type, *params])
    raw_rows = await cur.fetchall()
    structured_flows = await review_flows_by_rows(
        cur, [(parser_type, str(raw[1])) for raw in raw_rows]
    )
    events_by_flow = await review_events_by_flow_ids(
        cur,
        [int(flow["id"]) for flow in structured_flows.values() if flow.get("id")],
    )
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        values = json.loads(raw[7] or "{}")
        workflow = TASK_WORKFLOWS[parser_type]
        summary = workflow.summary(values)
        result = str(values.get(workflow.result_field) or "").strip()
        source_count = int(raw[9] or 0)
        projection_conflict = bool(raw[10])
        decision = str(raw[11] or "")
        category = stage = reason = ""
        eligible = False
        flow = structured_flows.get((parser_type, str(raw[1])))
        export_review = review_export_fields(
            events_by_flow.get(int(flow["id"]), []) if flow else []
        )
        if flow and flow.get("state") == FINAL_UNVERIFIABLE:
            flow_matches_source = (
                int(flow.get("source_id") or 0) == int(raw[0])
                and int(flow.get("source_revision") or 0) == int(raw[2])
                and str(flow.get("source_row_hash") or "") == str(raw[3] or "")
                and not str(flow.get("safe_reason_code") or "")
            )
            if result == "无法核实" and flow_matches_source:
                stage, category, reason, eligible = (
                    "direct", "无法核实", "已完成两级研判，达到最终无法核实", True,
                )
            else:
                stage = "review"
                reason = (
                    "最终研判后的本地任务来源已变化，请重新核对"
                    if local_data_source_enabled()
                    else "最终研判后的腾讯来源已变化，需重新对账后才能导出"
                )
        elif result in ARCHIVE_RESULTS and parser_type == "全链条":
            category, stage, reason, eligible = result, "direct", "核查结果可直接反馈归档", True
        elif parser_type == "全链条" and result == "移交":
            stage, reason = "review", "历史移交结果未区分所内或所外"
            if decision == "transfer_external":
                category, eligible = "移交（所外）", True
        elif parser_type == "全链条" and result == "移交（所内）":
            stage, reason = "review", "所内移交需基础管控审核"
            if decision == "archive":
                category, eligible = "移交（所内）", True
        elif result == "无法核实":
            stage = "review"
            if flow:
                reason = str(flow.get("state_label") or "正在执行两级研判流程")
                if flow.get("safe_reason_code"):
                    reason += "；来源状态异常，已暂停自动流转"
            else:
                reason = "等待系统建立两级研判流程"
        elif parser_type == "全链条" and result == "已登记":
            stage, category = "registered", "已登记"
            registration_status = str(raw[13] or "")
            confirmed_at = raw[14] if isinstance(raw[14], datetime) else None
            if registration_status != "confirmed":
                reason = "历史已登记，缺少居住证自动确认记录，待人工确认归档"
            elif confirmed_at is None:
                reason = "居住证自动确认时间缺失，待人工确认归档"
            elif int(raw[15] or 0) != int(raw[0]):
                reason = "确认后的任务来源已变化，需重新复核"
            elif int(raw[16] or 0) != int(raw[2]) or str(raw[17] or "") != str(raw[3] or ""):
                reason = "确认后的来源版本或内容已变化，需重新复核"
            elif not raw[8] or str(raw[18] or "") != str(raw[8] or ""):
                reason = "确认后的核查对象已变化，需重新复核"
            elif str(raw[19] or "").strip() != str(raw[26] or "").strip():
                reason = "确认后的任务社区已变化，需重新复核"
            elif raw[20] is None:
                reason = "确认记录缺少关联房屋，待人工复核"
            elif str(raw[22] or "") != "active":
                reason = "关联房屋已停用，需重新复核"
            elif int(raw[21] or 0) != int(raw[23] or 0):
                reason = "关联房屋档案已更新，需重新复核"
            elif bool(raw[24]):
                reason = "腾讯写回尚未完成或存在冲突，暂不能归档"
            elif registration_archive_ready(confirmed_at):
                eligible, reason = True, "居住证已自动确认，且已完整保留 24 小时"
            else:
                reason = "居住证已自动确认，需完整保留 24 小时后归档"
        if stage and (source_count != 1 or projection_conflict):
            eligible = False
            conflict_reason = "存在重复或冲突来源行，需先完成来源核查"
            reason = f"{reason}；{conflict_reason}" if reason else conflict_reason
        if stage and bool(raw[24]):
            eligible = False
            pending_reason = "存在待同步或冲突修改，需先完成腾讯写回"
            if pending_reason not in reason:
                reason = f"{reason}；{pending_reason}" if reason else pending_reason
        if stage and bool(raw[25]):
            eligible = False
            archive_reason = "已进入其他未终结归档批次，不能重复导出"
            reason = f"{reason}；{archive_reason}" if reason else archive_reason
        if stage:
            confirmed_at = raw[14] if isinstance(raw[14], datetime) else None
            archive_available_at = registration_archive_available_at(confirmed_at)
            candidate = {
                "source_id": int(raw[0]), "row_key": str(raw[1]), "revision": int(raw[2]),
                "row_hash": str(raw[3]), "physical_row": int(raw[4]),
                "spreadsheet_id": int(raw[5]), "sheet_id": str(raw[6]),
                "name": summary["title"], "identity": summary["identity_number"],
                "phone": summary["phone"], "address": summary["address"],
                "source": summary["source"], "registration": summary["registration_status"],
                "result": result, "deadline": summary["deadline"],
                "stage": stage, "category": category, "eligible": eligible,
                "reason": reason, "decision": decision, "review_note": str(raw[12] or ""),
                "source_count": source_count, "conflict": projection_conflict,
                "registration_status": str(raw[13] or ""),
                "registration_confirmed_at": confirmed_at.isoformat() + "Z" if confirmed_at else None,
                "archive_available_at": archive_available_at.isoformat() + "Z" if archive_available_at else None,
                "registration_identity_hmac": str(raw[18] or ""),
                "registration_property_id": int(raw[20]) if raw[20] is not None else None,
                "registration_property_version": int(raw[21]) if raw[21] is not None else None,
                "candidate_rule_version": (
                    REGISTRATION_ARCHIVE_RULE_VERSION if stage == "registered" else ""
                ),
                **export_review,
            }
            if include_source_values:
                candidate["_source_values_json"] = stable_json(values)
                candidate["_registration_confirmed_at_db"] = confirmed_at
            rows.append(candidate)
    return rows


def _filter_candidate_rows(rows: list[dict[str, Any]], data: CandidateSearch) -> list[dict[str, Any]]:
    keyword = data.keyword.strip().lower()
    return [
        row for row in rows
        if row["stage"] in data.stages
        and (
            not keyword
            or keyword in "\n".join(
                [row["name"], row["identity"], row["phone"], row["address"]]
            ).lower()
        )
    ]


@router.post("/police-raw/preview")
async def preview_police_raw(user: dict = Depends(require_fullchain_archive)):
    del user
    raise HTTPException(status_code=410, detail=POLICE_RAW_RETIRED_MESSAGE)


@router.post("/police-raw/confirm")
async def confirm_police_raw(
    user: dict = Depends(require_fullchain_archive),
):
    del user
    raise HTTPException(status_code=410, detail=POLICE_RAW_RETIRED_MESSAGE)


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
        rows = await _candidate_rows(cur, data.parser_type)
    rows = _filter_candidate_rows(rows, data)
    start = (data.page - 1) * data.page_size
    return {"data": rows[start:start + data.page_size], "total": len(rows), "page": data.page, "page_size": data.page_size, "counts": {stage: sum(row["stage"] == stage for row in rows) for stage in ("direct", "review", "registered")}}


@router.post("/candidates/selection")
async def select_candidates(data: CandidateSearch, user: dict = Depends(require_fullchain_archive), conn=Depends(get_db)):
    """Return only selectable IDs for the current filter, without reloading row details."""
    del user
    async with conn.cursor() as cur:
        rows = await _candidate_rows(cur, data.parser_type)
    filtered = _filter_candidate_rows(rows, data)
    source_ids = [row["source_id"] for row in filtered if row["eligible"]]
    if len(source_ids) > settings.FULLCHAIN_ARCHIVE_MAX_ROWS:
        raise HTTPException(400, f"当前筛选下有 {len(source_ids)} 条可选数据，单次最多选择 {settings.FULLCHAIN_ARCHIVE_MAX_ROWS} 条")
    return {"source_ids": source_ids, "total": len(source_ids), "max_total": settings.FULLCHAIN_ARCHIVE_MAX_ROWS}


@router.post("/reviews")
async def save_review(data: ReviewDecision, request: Request, user: dict = Depends(require_fullchain_archive), conn=Depends(get_db)):
    if data.parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "不支持的业务类型")
    if data.parser_type != "全链条":
        raise HTTPException(400, "该业务的最终无法核实数据无需人工归档审核")
    async with conn.cursor() as cur:
        await cur.execute("SELECT values_json FROM _online_source_projection WHERE parser_type=%s AND row_key=%s", (data.parser_type, data.row_key))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(409, "任务已经变化，请刷新")
        values = json.loads(row[0] or "{}")
        result = str(values.get(TASK_WORKFLOWS[data.parser_type].result_field) or "").strip()
        allowed = {"移交": {"transfer_internal", "transfer_external"}, "移交（所内）": {"keep", "archive"}}
        if data.decision not in allowed.get(result, set()):
            raise HTTPException(409, "当前核查结果不允许使用该审核决定")
        await cur.execute("""
            INSERT INTO _fullchain_archive_reviews (parser_type,row_key,decision,note,decided_by)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE decision=VALUES(decision),note=VALUES(note),decided_by=VALUES(decided_by),decided_at=UTC_TIMESTAMP()
        """, (data.parser_type, data.row_key, data.decision, data.note.strip(), user["id"]))
    await record_admin_audit(user, "fullchain.archive.review", target_type="online_task", target_name=f"{data.parser_type}:{data.row_key}", detail={"decision": data.decision}, **request_audit_fields(request))
    return {"message": "审核决定已保存"}


@router.post("/exports/preview")
async def preview_export(
    source_ids: list[int],
    parser_type: str = Query("全链条"),
    user: dict = Depends(require_fullchain_archive),
    conn=Depends(get_db),
):
    del user
    ids = list(dict.fromkeys(source_ids))
    if not ids or len(ids) > settings.FULLCHAIN_ARCHIVE_MAX_ROWS:
        raise HTTPException(400, "请选择 1 至 5000 条数据")
    async with conn.cursor() as cur:
        rows = await _candidate_rows(cur, parser_type, ids)
    eligible = [row for row in rows if row["eligible"]]
    eligible_ids = {row["source_id"] for row in eligible}
    if eligible_ids != set(ids):
        raise HTTPException(409, "部分数据尚不满足归档条件，请刷新后重新选择")
    return {"total": len(eligible), "categories": {category: sum(row["category"] == category for row in eligible) for category in sorted({row["category"] for row in eligible})}, "rows": [{key: row[key] for key in ("source_id", "name", "result", "category", "reason")} for row in eligible[:100]], "preview_token": _preview_token(eligible)}


@router.post("/exports", status_code=202)
async def create_export(
    data: ExportSelection,
    request: Request,
    parser_type: str = Query("全链条"),
    user: dict = Depends(require_fullchain_archive),
    conn=Depends(get_db),
):
    ids = list(dict.fromkeys(data.source_ids))
    async with conn.cursor() as cur:
        rows = await _candidate_rows(
            cur, parser_type, ids, include_source_values=True
        )
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
                (export_no,parser_type,status,phase,file_name,storage_key,total_count,categories_json,requested_by)
                VALUES (%s,%s,'queued','preparing','', '', %s,%s,%s)
            """, (export_no, parser_type, len(eligible), stable_json(categories), user["id"]))
            export_id = int(cur.lastrowid)
            filename = f"{parser_type}反馈归档-{export_no}.xlsx"
            storage_key = f"export-{export_id}.xlsx"
            content = build_archive_workbook(
                eligible, export_id, datetime.now(), parser_type=parser_type
            )
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
                (export_id,parser_type,row_key,source_id,spreadsheet_id,sheet_id,
                 physical_row,expected_revision,expected_row_hash,source_values_json,
                 registration_confirmed_at,registration_status,
                 registration_identity_hmac,registration_property_id,
                 registration_property_version,candidate_rule_version,category)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, [(
                export_id, parser_type, row["row_key"], row["source_id"],
                row["spreadsheet_id"], row["sheet_id"], row["physical_row"],
                row["revision"], row["row_hash"], row["_source_values_json"],
                row["_registration_confirmed_at_db"], row["registration_status"],
                row["registration_identity_hmac"], row["registration_property_id"],
                row["registration_property_version"], row["candidate_rule_version"],
                row["category"],
            ) for row in eligible])
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
async def list_exports(
    parser_type: str | None = Query(None),
    user: dict = Depends(require_fullchain_archive),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        if parser_type:
            if parser_type not in TASK_WORKFLOWS:
                raise HTTPException(400, "不支持的业务类型")
            await cur.execute(
                "SELECT id FROM _fullchain_archive_exports WHERE parser_type=%s "
                "ORDER BY id DESC LIMIT 100",
                (parser_type,),
            )
        else:
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
