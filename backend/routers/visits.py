"""走访导入、星级关联、覆盖范围和区间汇总接口。"""

import asyncio
from datetime import date
from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile

from database import get_db
from deps import require_admin
from services.audit import record_admin_audit, request_audit_fields
from services.business_time import get_business_timezone_name
from services.star_rating_import import (
    import_star_rating_workbook,
    parse_star_rating_workbook,
)
from services.visit_summary import get_visit_summary
from services.visit_import import (
    ISSUE_PAGE_SIZE,
    MAX_FILE_BYTES,
    VISIT_IMPORT_LOCK_NAME,
    ImportIssue,
    VisitWorkbookError,
    create_import_batch,
    fail_import_batch,
    find_duplicate_batch,
    get_visit_coverage,
    import_parsed_workbook,
    list_import_issues,
    parse_visit_workbook,
)

router = APIRouter(prefix="/api/visits", tags=["走访汇总"])


def _safe_filename(filename: str | None) -> str:
    normalized = (filename or "走访明细.xlsx").replace("\\", "/")
    return Path(normalized).name[:255]


async def _result_with_details(conn, result: dict) -> dict:
    result["coverage"] = await get_visit_coverage(conn)
    batch_id = result.get("batch_id")
    if batch_id and result.get("status") != "duplicate":
        result["issues"] = await list_import_issues(
            conn,
            int(batch_id),
            page=1,
            page_size=ISSUE_PAGE_SIZE,
        )
    else:
        result["issues"] = {
            "data": [],
            "total": 0,
            "page": 1,
            "page_size": ISSUE_PAGE_SIZE,
        }
    return result


async def _try_acquire_import_lock(conn) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT GET_LOCK(%s, 0)",
            (VISIT_IMPORT_LOCK_NAME,),
        )
        row = await cur.fetchone()
    return bool(row and row[0] == 1)


async def _release_import_lock(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT RELEASE_LOCK(%s)",
            (VISIT_IMPORT_LOCK_NAME,),
        )


@router.get("/coverage")
async def coverage(conn=Depends(get_db)):
    return await get_visit_coverage(conn)


@router.get("/summary")
async def summary(
    start_date: date = Query(...),
    end_date: date = Query(...),
    conn=Depends(get_db),
):
    if start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="开始日期不能晚于结束日期",
        )
    return await get_visit_summary(conn, start_date, end_date)


@router.post("/imports/detail")
async def import_visit_detail(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    filename = _safe_filename(file.filename)
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(status_code=400, detail="只支持 .xlsx 文件")

    content = await file.read(MAX_FILE_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="XLSX 文件不能超过 20MB")

    if not await _try_acquire_import_lock(conn):
        raise HTTPException(
            status_code=409,
            detail="另一份走访明细正在导入，请稍后再试",
        )
    try:
        file_sha256 = sha256(content).hexdigest()
        duplicate = await find_duplicate_batch(conn, file_sha256)
        if duplicate:
            await record_admin_audit(
                user,
                "visit_detail.import",
                target_type="visit_import",
                target_name=str(duplicate["batch_id"]),
                result="duplicate",
                detail={"duplicate_file": True},
                **request_audit_fields(request),
            )
            return await _result_with_details(conn, duplicate)

        batch_id = await create_import_batch(
            conn,
            filename=filename,
            file_sha256=file_sha256,
            file_size=len(content),
            uploader_id=user["id"],
        )
        try:
            async with conn.cursor() as cur:
                timezone_name = await get_business_timezone_name(cur)
            parsed = await asyncio.to_thread(
                parse_visit_workbook,
                content,
                timezone_name,
            )
            result = await import_parsed_workbook(
                conn,
                batch_id=batch_id,
                parsed=parsed,
            )
        except VisitWorkbookError as exc:
            issue = ImportIssue(
                severity="error",
                code="invalid_workbook",
                row_number=0,
                message=str(exc),
                row_preview={},
            )
            await fail_import_batch(conn, batch_id, str(exc), [issue])
            result = {
                "batch_id": batch_id,
                "import_type": "detail",
                "status": "failed",
                "duplicate_file": False,
                "file_start_date": None,
                "file_end_date": None,
                "overlap_start_date": None,
                "overlap_end_date": None,
                "inserted_rows": 0,
                "updated_rows": 0,
                "unchanged_rows": 0,
                "ignored_rows": 0,
                "error_count": 1,
                "warning_count": 0,
                "message": str(exc),
            }
        except Exception:
            await fail_import_batch(
                conn,
                batch_id,
                "走访明细入库失败，请稍后重试",
            )
            await record_admin_audit(
                user,
                "visit_detail.import",
                target_type="visit_import",
                target_name=str(batch_id),
                result="failed",
                detail={"reason": "internal_error"},
                **request_audit_fields(request),
            )
            raise

        await record_admin_audit(
            user,
            "visit_detail.import",
            target_type="visit_import",
            target_name=str(batch_id),
            result=result["status"],
            detail={
                "inserted_rows": result["inserted_rows"],
                "updated_rows": result["updated_rows"],
                "unchanged_rows": result["unchanged_rows"],
                "ignored_rows": result["ignored_rows"],
                "error_count": result["error_count"],
                "warning_count": result["warning_count"],
            },
            **request_audit_fields(request),
        )
        return await _result_with_details(conn, result)
    finally:
        await _release_import_lock(conn)


@router.post("/imports/rating")
async def import_star_rating(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    filename = _safe_filename(file.filename)
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(status_code=400, detail="只支持 .xlsx 文件")

    content = await file.read(MAX_FILE_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="XLSX 文件不能超过 20MB")

    if not await _try_acquire_import_lock(conn):
        raise HTTPException(
            status_code=409,
            detail="另一份走访或星级文件正在导入，请稍后再试",
        )
    try:
        file_sha256 = sha256(content).hexdigest()
        duplicate = await find_duplicate_batch(
            conn,
            file_sha256,
            import_type="rating",
            include_partial=False,
        )
        if duplicate:
            await record_admin_audit(
                user,
                "visit_rating.import",
                target_type="visit_import",
                target_name=str(duplicate["batch_id"]),
                result="duplicate",
                detail={"duplicate_file": True},
                **request_audit_fields(request),
            )
            return await _result_with_details(conn, duplicate)

        batch_id = await create_import_batch(
            conn,
            filename=filename,
            file_sha256=file_sha256,
            file_size=len(content),
            uploader_id=user["id"],
            import_type="rating",
        )
        try:
            async with conn.cursor() as cur:
                timezone_name = await get_business_timezone_name(cur)
            parsed = await asyncio.to_thread(
                parse_star_rating_workbook,
                content,
                timezone_name,
            )
            result = await import_star_rating_workbook(
                conn,
                batch_id=batch_id,
                parsed=parsed,
            )
        except VisitWorkbookError as exc:
            issue = ImportIssue(
                severity="error",
                code="invalid_star_rating_workbook",
                row_number=0,
                message=str(exc),
                row_preview={},
            )
            await fail_import_batch(conn, batch_id, str(exc), [issue])
            result = {
                "batch_id": batch_id,
                "import_type": "rating",
                "status": "failed",
                "duplicate_file": False,
                "file_start_date": None,
                "file_end_date": None,
                "overlap_start_date": None,
                "overlap_end_date": None,
                "inserted_rows": 0,
                "updated_rows": 0,
                "unchanged_rows": 0,
                "ignored_rows": 0,
                "matched_rows": 0,
                "unmatched_rows": 0,
                "ambiguous_rows": 0,
                "error_count": 1,
                "warning_count": 0,
                "message": str(exc),
            }
        except Exception:
            await fail_import_batch(
                conn,
                batch_id,
                "星级评定关联失败，请稍后重试",
            )
            await record_admin_audit(
                user,
                "visit_rating.import",
                target_type="visit_import",
                target_name=str(batch_id),
                result="failed",
                detail={"reason": "internal_error"},
                **request_audit_fields(request),
            )
            raise

        await record_admin_audit(
            user,
            "visit_rating.import",
            target_type="visit_import",
            target_name=str(batch_id),
            result=result["status"],
            detail={
                "inserted_rows": result["inserted_rows"],
                "updated_rows": result["updated_rows"],
                "unchanged_rows": result["unchanged_rows"],
                "ignored_rows": result["ignored_rows"],
                "unmatched_rows": result.get("unmatched_rows", 0),
                "ambiguous_rows": result.get("ambiguous_rows", 0),
                "error_count": result["error_count"],
                "warning_count": result["warning_count"],
            },
            **request_audit_fields(request),
        )
        return await _result_with_details(conn, result)
    finally:
        await _release_import_lock(conn)


@router.get("/imports/{batch_id}/issues")
async def import_issues(
    batch_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM _visit_import_batches WHERE id=%s",
            (batch_id,),
        )
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="导入批次不存在")
    return await list_import_issues(
        conn,
        batch_id,
        page=page,
        page_size=page_size,
    )
