"""全民防模型三来源同步接口。"""

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from database import get_db
from deps import require_permission
from services.audit import record_admin_audit, request_audit_fields
from services.external_acquisition_jobs import create_job
from services.permissions import SYNC_TRIGGER
from services.qmf_source_sync import run_qmf_source_sync
from services.model_three_self_owned import (
    SelfOwnedImportError,
    apply_self_owned_import,
    latest_batch,
    parse_self_owned_zip,
)

router = APIRouter(prefix="/api/qmf-source", tags=["全民防同步"])


@router.post("/sync")
async def start_qmf_source_sync(
    request: Request,
    user: dict = Depends(require_permission(SYNC_TRIGGER)),
):
    job, reused = await create_job(
        "qmf_source",
        int(user["id"]),
        {"source": "legacy-model-three", "mode": "pending-only"},
        run_qmf_source_sync,
        dedupe_key="current",
    )
    await record_admin_audit(
        user,
        "qmf_source.sync",
        target_type="qmf_source",
        target_name="疑似未注销模型三",
        detail={"run_id": job.get("id"), "reused": reused},
        **request_audit_fields(request),
    )
    return {"data": job, "reused": reused}


@router.post("/self-owned/import")
async def import_self_owned_roster(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_permission(SYNC_TRIGGER)),
    conn=Depends(get_db),
):
    """导入辖区自购自住人员资产资料，并更新平台内匹配任务。"""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "自购自住名单只支持 ZIP 文件")
    try:
        content = await file.read()
        parsed = parse_self_owned_zip(content)
        result = await apply_self_owned_import(
            conn,
            parsed=parsed,
            file_name=file.filename or "自购自住名单.zip",
            user_id=int(user["id"]),
        )
    except SelfOwnedImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - do not expose source details
        raise HTTPException(500, "自购自住名单导入失败，请稍后重试") from exc
    await record_admin_audit(
        user,
        "qmf.self_owned.import",
        target_type="qmf_self_owned_batch",
        target_name="辖区自购自住人员资产资料",
        detail={
            "batch_id": result["batch_id"],
            "total_rows": result["total_rows"],
            "valid_rows": result["valid_rows"],
            "matched_tasks": result["matched_tasks"],
            "updated_tasks": result["updated_tasks"],
            "skipped_tasks": result["skipped_tasks"],
            "registry_people_created": result["registry_people_created"],
            "registry_people_reused": result["registry_people_reused"],
            "registry_phones_created": result["registry_phones_created"],
            "tag_assignments_created": result["tag_assignments_created"],
        },
        **request_audit_fields(request),
    )
    return {"data": result}


@router.get("/self-owned/latest")
async def get_latest_self_owned_roster(
    user: dict = Depends(require_permission(SYNC_TRIGGER)),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        return {"data": await latest_batch(cur)}
