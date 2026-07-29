"""日报 API - 生成和查看分汇总表 + 总汇总表"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import get_current_user
from services.business_time import get_business_date_from_db
from services.stats_calculator import DailyReportBuilder
from services.report_builders import IMPLEMENTED_TYPES
from services.report_builders.summary import (
    build_summary_with_subreports,
    get_summary,
)
from services.report_range import get_report_range, get_summary_range
from services.report_overview import get_online_overview
from services.report_view import project_report_payload

router = APIRouter(prefix="/api/stats", tags=["统计查询"])
builder = DailyReportBuilder()

# 支持的分汇总表类型 + 总汇总表
REPORT_TYPES = ["全链条", "出租房屋核查", "寄递业", "疑似未注销模型三", "疑似返苏", "总汇总表"]
# 分表已实现的类型
IMPLEMENTED_SUBTYPES = [t for t in IMPLEMENTED_TYPES] + ["总汇总表"]


def _column_mode(
    requested_mode: Optional[Literal["two", "three"]],
    user: dict,
) -> Literal["two", "three"]:
    if requested_mode:
        return requested_mode
    return "two" if user.get("report_column_mode") == "two" else "three"


@router.get("/types")
async def get_types():
    """获取分汇总表类型列表"""
    return {"data": REPORT_TYPES, "implemented": IMPLEMENTED_SUBTYPES}


@router.get("/overview")
async def get_overview(
    start_date: str = Query(..., description="yyyy-MM-dd"),
    end_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
):
    """读取跟随当前业务类型和日期区间变化的数据概览。"""
    try:
        return await get_online_overview(
            start_date,
            end_date,
            parser_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/build")
async def build_report(
    report_date: Optional[str] = Query(None, description="yyyy-MM-dd，默认今天"),
    parser_type: str = Query("全链条"),
):
    """手动触发日报生成"""
    d = report_date or (await get_business_date_from_db()).isoformat()
    if parser_type == "总汇总表":
        result = await build_summary_with_subreports(d)
        if not result.get("implemented"):
            return {"message": result.get("message", ""), "implemented": False}
        return {"message": "分汇总表和总汇总表生成成功", **result}
    result = await builder.build(d, parser_type)
    if not result.get("implemented"):
        return {"message": result.get("message", "未实现"), "implemented": False}
    return {"message": "日报生成成功", **result}


@router.get("/report")
async def get_report(
    report_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
    column_mode: Optional[Literal["two", "three"]] = Query(None),
    user: dict = Depends(get_current_user),
):
    """查看指定日期的分汇总表或总汇总表"""
    if parser_type == "总汇总表":
        result = await get_summary(report_date)
    else:
        result = await builder.get_report(report_date, parser_type)
    return project_report_payload(result, _column_mode(column_mode, user))


@router.get("/report_range")
async def get_report_range_endpoint(
    start_date: str = Query(..., description="yyyy-MM-dd"),
    end_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
    column_mode: Optional[Literal["two", "three"]] = Query(None),
    user: dict = Depends(get_current_user),
):
    """按时间区间查看汇总表（任务流水去重后重算比例）。"""
    try:
        if start_date > end_date:
            return {"exists": False, "message": "起始日期不能晚于结束日期"}
        if parser_type == "总汇总表":
            result = await get_summary_range(start_date, end_date)
        else:
            result = await get_report_range(start_date, end_date, parser_type)
        return project_report_payload(result, _column_mode(column_mode, user))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"exists": False, "message": f"服务器错误：{e}"}


@router.get("/reports")
async def list_reports():
    """列出所有已生成的日报"""
    return {"data": await builder.list_reports()}


@router.get("/today")
async def get_today_report(
    parser_type: str = Query("全链条"),
    column_mode: Optional[Literal["two", "three"]] = Query(None),
    user: dict = Depends(get_current_user),
):
    """获取今天的日报"""
    today = (await get_business_date_from_db()).isoformat()
    if parser_type == "总汇总表":
        result = await get_summary(today)
    else:
        result = await builder.get_report(today, parser_type)
    return project_report_payload(result, _column_mode(column_mode, user))
