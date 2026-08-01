"""日报 API - 生成和查看分汇总表 + 总汇总表"""

import json
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import get_db
from deps import get_current_user, require_permission
from services.audit import record_admin_audit, request_audit_fields
from services.business_time import get_business_date_from_db
from services.stats_calculator import DailyReportBuilder
from services.report_builders import IMPLEMENTED_TYPES
from services.report_builders.summary import get_summary
from services.report_range import get_report_range, get_summary_range
from services.report_overview import get_online_overview
from services.report_view import project_report_payload
from services.data_scope import (
    allowed_community_names,
    filter_report_payload,
)
from services.permissions import (
    ONLINE_SUMMARY_VIEW,
    REPORT_CONFIG_MANAGE,
)

router = APIRouter(prefix="/api/stats", tags=["统计查询"])
builder = DailyReportBuilder()

# 支持的分汇总表类型 + 总汇总表
REPORT_TYPES = ["全链条", "出租房屋核查", "寄递业", "疑似未注销模型三", "疑似返苏", "总汇总表"]
# 分表已实现的类型
IMPLEMENTED_SUBTYPES = [t for t in IMPLEMENTED_TYPES] + ["总汇总表"]


class SummaryConfigUpdate(BaseModel):
    types: list[str] = Field(min_length=1)


def _normalize_summary_types(raw_types: list[str]) -> list[str]:
    selected: list[str] = []
    for raw_type in raw_types:
        parser_type = str(raw_type).strip()
        if parser_type not in IMPLEMENTED_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"“{parser_type}”不支持生成分汇总表",
            )
        if parser_type not in selected:
            selected.append(parser_type)
    if not selected:
        raise HTTPException(status_code=400, detail="至少选择一种分汇总表")
    return selected


async def _read_summary_types(cur) -> list[str]:
    await cur.execute(
        "SELECT config_value FROM _system_config "
        "WHERE config_key='summary_types'"
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return list(IMPLEMENTED_TYPES)
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return list(IMPLEMENTED_TYPES)
    if not isinstance(value, list):
        return list(IMPLEMENTED_TYPES)
    valid = [
        parser_type
        for parser_type in IMPLEMENTED_TYPES
        if parser_type in value
    ]
    return valid or list(IMPLEMENTED_TYPES)


def _column_mode(
    requested_mode: Optional[Literal["two", "three"]],
    user: dict,
) -> Literal["two", "three"]:
    if requested_mode:
        return requested_mode
    return "two" if user.get("report_column_mode") == "two" else "three"


@router.get("/types")
async def get_types(
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
):
    """获取分汇总表类型列表"""
    del user
    return {"data": REPORT_TYPES, "implemented": IMPLEMENTED_SUBTYPES}


@router.get("/summary-config")
async def get_summary_config(
    user: dict = Depends(require_permission(REPORT_CONFIG_MANAGE)),
    conn=Depends(get_db),
):
    """读取总汇总表包含的分表类型，管理员和超级管理员可用。"""
    del user
    async with conn.cursor() as cur:
        selected = await _read_summary_types(cur)
    return {
        "available_types": list(IMPLEMENTED_TYPES),
        "selected_types": selected,
    }


@router.put("/summary-config")
async def update_summary_config(
    data: SummaryConfigUpdate,
    request: Request,
    user: dict = Depends(require_permission(REPORT_CONFIG_MANAGE)),
    conn=Depends(get_db),
):
    """保存总汇总表包含的分表类型。"""
    selected = _normalize_summary_types(data.types)
    serialized = json.dumps(selected, ensure_ascii=False)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO _system_config (config_key, config_value) "
            "VALUES ('summary_types', %s) "
            "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)",
            (serialized,),
        )
    await record_admin_audit(
        user,
        "report.summary_config.update",
        target_type="system_config",
        target_name="summary_types",
        detail={"types": selected},
        **request_audit_fields(request),
    )
    return {
        "available_types": list(IMPLEMENTED_TYPES),
        "selected_types": selected,
        "message": "总汇总表配置已保存",
    }


@router.get("/overview")
async def get_overview(
    start_date: str = Query(..., description="yyyy-MM-dd"),
    end_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
):
    """读取跟随当前业务类型和日期区间变化的数据概览。"""
    try:
        return await get_online_overview(
            start_date,
            end_date,
            parser_type,
            await allowed_community_names(user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/report")
async def get_report(
    report_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
    column_mode: Optional[Literal["two", "three"]] = Query(None),
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
):
    """查看指定日期的分汇总表或总汇总表"""
    if parser_type == "总汇总表":
        result = await get_summary(report_date)
    else:
        result = await builder.get_report(report_date, parser_type)
    result = filter_report_payload(
        result,
        user,
        await allowed_community_names(user),
    )
    return project_report_payload(result, _column_mode(column_mode, user))


@router.get("/report_range")
async def get_report_range_endpoint(
    start_date: str = Query(..., description="yyyy-MM-dd"),
    end_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
    column_mode: Optional[Literal["two", "three"]] = Query(None),
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
):
    """按时间区间查看汇总表（任务流水去重后重算比例）。"""
    try:
        if start_date > end_date:
            return {"exists": False, "message": "起始日期不能晚于结束日期"}
        if parser_type == "总汇总表":
            result = await get_summary_range(start_date, end_date)
        else:
            result = await get_report_range(start_date, end_date, parser_type)
        result = filter_report_payload(
            result,
            user,
            await allowed_community_names(user),
        )
        return project_report_payload(result, _column_mode(column_mode, user))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"exists": False, "message": f"服务器错误：{e}"}


@router.get("/reports")
async def list_reports(
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
):
    """列出所有已生成的日报"""
    del user
    return {"data": await builder.list_reports()}


@router.get("/today")
async def get_today_report(
    parser_type: str = Query("全链条"),
    column_mode: Optional[Literal["two", "three"]] = Query(None),
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
):
    """获取今天的日报"""
    today = (await get_business_date_from_db()).isoformat()
    if parser_type == "总汇总表":
        result = await get_summary(today)
    else:
        result = await builder.get_report(today, parser_type)
    result = filter_report_payload(
        result,
        user,
        await allowed_community_names(user),
    )
    return project_report_payload(result, _column_mode(column_mode, user))
