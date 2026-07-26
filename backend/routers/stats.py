"""日报 API - 生成和查看分汇总表 + 总汇总表"""

from datetime import date
from typing import Optional
from fastapi import APIRouter, Query
from services.stats_calculator import DailyReportBuilder
from services.report_builders import IMPLEMENTED_TYPES
from services.report_builders.summary import build_summary, get_summary
from services.report_range import get_report_range, get_summary_range

router = APIRouter(prefix="/api/stats", tags=["统计查询"])
builder = DailyReportBuilder()

# 支持的分汇总表类型 + 总汇总表
REPORT_TYPES = ["全链条", "出租房屋核查", "寄递业", "疑似未注销模型三", "疑似返苏", "总汇总表"]
# 分表已实现的类型
IMPLEMENTED_SUBTYPES = [t for t in IMPLEMENTED_TYPES] + ["总汇总表"]


@router.get("/types")
async def get_types():
    """获取分汇总表类型列表"""
    return {"data": REPORT_TYPES, "implemented": IMPLEMENTED_SUBTYPES}


@router.post("/build")
async def build_report(
    report_date: Optional[str] = Query(None, description="yyyy-MM-dd，默认今天"),
    parser_type: str = Query("全链条"),
):
    """手动触发日报生成"""
    d = report_date or date.today().isoformat()
    if parser_type == "总汇总表":
        result = await build_summary(d)
        if not result.get("implemented"):
            return {"message": result.get("message", ""), "implemented": False}
        return {"message": "总汇总表生成成功", **result}
    result = await builder.build(d, parser_type)
    if not result.get("implemented"):
        return {"message": result.get("message", "未实现"), "implemented": False}
    return {"message": "日报生成成功", **result}


@router.get("/report")
async def get_report(
    report_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
):
    """查看指定日期的分汇总表或总汇总表"""
    if parser_type == "总汇总表":
        return await get_summary(report_date)
    return await builder.get_report(report_date, parser_type)


@router.get("/report_range")
async def get_report_range_endpoint(
    start_date: str = Query(..., description="yyyy-MM-dd"),
    end_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
):
    """按时间区间聚合查看分汇总表或总汇总表（跨日 SUM + 比率重算）"""
    try:
        if start_date > end_date:
            return {"exists": False, "message": "起始日期不能晚于结束日期"}
        if parser_type == "总汇总表":
            return await get_summary_range(start_date, end_date)
        return await get_report_range(start_date, end_date, parser_type)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"exists": False, "message": f"服务器错误：{e}"}


@router.get("/reports")
async def list_reports():
    """列出所有已生成的日报"""
    return {"data": await builder.list_reports()}


@router.get("/today")
async def get_today_report(parser_type: str = Query("全链条")):
    """获取今天的日报"""
    today = date.today().isoformat()
    if parser_type == "总汇总表":
        return await get_summary(today)
    return await builder.get_report(today, parser_type)
