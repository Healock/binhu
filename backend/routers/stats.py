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
from services.report_overview import (
    get_online_overview,
    get_online_overview_details,
)
from services.report_view import project_report_payload
from services.data_scope import (
    allowed_community_names,
    community_names_for_scopes,
    community_scopes,
    filter_report_payload,
)
from services.dashboard_scope import (
    formal_community,
    member_position,
    requested_responsibility_communities,
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
ScopeMode = Literal["permission", "responsibility"]


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


async def _requested_formal_communities(
    cur,
    user: dict,
    scope: ScopeMode,
    community: str,
) -> list[str] | None:
    if scope == "responsibility":
        try:
            return await requested_responsibility_communities(
                cur, user, ONLINE_SUMMARY_VIEW, community
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
    allowed = community_scopes(user, ONLINE_SUMMARY_VIEW)
    requested = str(community or "").strip()
    if not requested:
        return allowed
    formal = await formal_community(cur, requested)
    if not formal or (allowed is not None and formal not in allowed):
        raise HTTPException(403, "所选社区超出当前账号的数据范围")
    return [formal]


async def _overview_community_names(conn, formal: list[str] | None):
    return (
        await community_names_for_scopes(conn, formal)
        if formal is not None
        else None
    )


async def _formal_communities_for_endpoint(
    conn,
    user: dict,
    scope: ScopeMode,
    community: str,
) -> list[str] | None:
    """兼容内部直接调用；真实 HTTP 请求始终由依赖注入数据库连接。"""
    normalized_scope: ScopeMode = (
        scope
        if isinstance(scope, str) and scope in {"permission", "responsibility"}
        else "permission"
    )
    normalized_community = community if isinstance(community, str) else ""
    if not hasattr(conn, "cursor"):
        if normalized_scope != "permission" or normalized_community.strip():
            raise RuntimeError("职责范围请求需要数据库连接")
        return community_scopes(user, ONLINE_SUMMARY_VIEW)
    async with conn.cursor() as cur:
        return await _requested_formal_communities(
            cur, user, normalized_scope, normalized_community
        )


def _responsibility_inspector(user: dict, scope: ScopeMode) -> str | None:
    if scope != "responsibility" or member_position(user) != "组员":
        return None
    return str((user.get("member") or {}).get("name") or "").strip() or None


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
    scope: ScopeMode = Query("permission"),
    community: str = Query("", max_length=100),
    conn=Depends(get_db),
):
    """读取跟随当前业务类型和日期区间变化的数据概览。"""
    try:
        async with conn.cursor() as cur:
            formal = await _requested_formal_communities(
                cur, user, scope, community
            )
        return await get_online_overview(
            start_date,
            end_date,
            parser_type,
            await _overview_community_names(conn, formal),
            inspector=_responsibility_inspector(user, scope),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/overview/details")
async def get_overview_details(
    start_date: str = Query(..., description="yyyy-MM-dd"),
    end_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
    category: Literal[
        "carryover", "new", "changed", "pending", "completed"
    ] = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
    scope: ScopeMode = Query("permission"),
    community: str = Query("", max_length=100),
    conn=Depends(get_db),
):
    """读取与概览卡片数量严格一致的任务明细。"""
    try:
        async with conn.cursor() as cur:
            formal = await _requested_formal_communities(
                cur, user, scope, community
            )
        return await get_online_overview_details(
            start_date,
            end_date,
            parser_type,
            category,
            page=page,
            page_size=page_size,
            community=await _overview_community_names(conn, formal),
            inspector=_responsibility_inspector(user, scope),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/report")
async def get_report(
    report_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
    column_mode: Optional[Literal["two", "three"]] = Query(None),
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
    scope: ScopeMode = Query("permission"),
    community: str = Query("", max_length=100),
    conn=Depends(get_db),
):
    """查看指定日期的分汇总表或总汇总表"""
    formal = await _formal_communities_for_endpoint(
        conn, user, scope, community
    )
    if parser_type == "总汇总表":
        result = await get_summary(report_date)
    else:
        result = await builder.get_report(report_date, parser_type)
    inspector = _responsibility_inspector(user, scope)
    result = filter_report_payload(
        result, user, formal,
        [inspector] if inspector else None,
    )
    return project_report_payload(result, _column_mode(column_mode, user))


@router.get("/report_range")
async def get_report_range_endpoint(
    start_date: str = Query(..., description="yyyy-MM-dd"),
    end_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
    column_mode: Optional[Literal["two", "three"]] = Query(None),
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
    scope: ScopeMode = Query("permission"),
    community: str = Query("", max_length=100),
    conn=Depends(get_db),
):
    """按时间区间查看汇总表（任务流水去重后重算比例）。"""
    try:
        formal = await _formal_communities_for_endpoint(
            conn, user, scope, community
        )
        if start_date > end_date:
            return {"exists": False, "message": "起始日期不能晚于结束日期"}
        if parser_type == "总汇总表":
            result = await get_summary_range(start_date, end_date)
        else:
            result = await get_report_range(start_date, end_date, parser_type)
        inspector = _responsibility_inspector(user, scope)
        result = filter_report_payload(
            result, user, formal,
            [inspector] if inspector else None,
        )
        return project_report_payload(result, _column_mode(column_mode, user))
    except HTTPException:
        raise
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
