"""Privacy-safe audit records for browser-generated XLSX exports."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from deps import get_current_user
from services.audit import record_admin_audit, request_audit_fields
from services.permissions import (
    ONLINE_SUMMARY_VIEW,
    VISIT_SUMMARY_VIEW,
    has_permission,
)


router = APIRouter(prefix="/api/exports", tags=["导出记录"])
ExportType = Literal["online_summary", "visit_summary", "code_summary"]

EXPORT_CONFIG = {
    "online_summary": {
        "permission": ONLINE_SUMMARY_VIEW,
        "action": "online_summary.export",
        "target_type": "online_summary",
        "allowed_types": {
            "全链条",
            "出租房屋核查",
            "寄递业",
            "疑似未注销模型三",
            "疑似返苏",
            "总汇总表",
        },
    },
    "visit_summary": {
        "permission": VISIT_SUMMARY_VIEW,
        "action": "visit_summary.export",
        "target_type": "visit_summary",
        "allowed_types": {"出租房", "自购房"},
    },
    "code_summary": {
        "permission": VISIT_SUMMARY_VIEW,
        "action": "code_summary.export",
        "target_type": "code_summary",
        "allowed_types": {"平安码", "管家码"},
    },
}


class XlsxExportAuditRequest(BaseModel):
    export_type: ExportType
    start_date: date
    end_date: date
    summary_type: str = Field(min_length=1, max_length=50)
    inspector_rows: int = Field(ge=0, le=1_000_000)
    community_rows: int = Field(ge=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_range_and_type(self):
        if self.end_date < self.start_date:
            raise ValueError("结束日期不能早于开始日期")
        allowed = EXPORT_CONFIG[self.export_type]["allowed_types"]
        if self.summary_type not in allowed:
            raise ValueError("导出类型无效")
        return self


@router.post("/xlsx")
async def record_xlsx_export(
    data: XlsxExportAuditRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    config = EXPORT_CONFIG[data.export_type]
    if not has_permission(user, str(config["permission"])):
        raise HTTPException(403, "当前权限组不能导出该汇总")

    await record_admin_audit(
        user,
        str(config["action"]),
        target_type=str(config["target_type"]),
        target_name=f"{data.start_date.isoformat()} 至 {data.end_date.isoformat()}",
        detail={
            "file_format": "XLSX",
            "summary_type": data.summary_type,
            "start_date": data.start_date.isoformat(),
            "end_date": data.end_date.isoformat(),
            "inspector_rows": data.inspector_rows,
            "community_rows": data.community_rows,
        },
        **request_audit_fields(request),
    )
    return {"recorded": True}
