"""全民防模型三来源同步接口。"""

from fastapi import APIRouter, Depends, Request

from deps import require_permission
from services.audit import record_admin_audit, request_audit_fields
from services.external_acquisition_jobs import create_job
from services.permissions import SYNC_TRIGGER
from services.qmf_source_sync import run_qmf_source_sync

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
