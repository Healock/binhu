"""日报 API - 生成和查看分汇总表 + 总汇总表"""

import json
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import get_db
from deps import get_current_user, require_permission, require_super_admin
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
from services.txdocs_statistics_monitor import (
    get_txdocs_statistics_overview,
    _load_monitor_credentials,
    _load_monitor_targets,
    ensure_txdocs_monitor_config_schema,
    monitoring_configuration_state,
    monitoring_environment_allowed,
    get_monitoring_runtime_state,
    run_txdocs_statistics_once,
)
from services.qmf_config import encrypt_secret
from services.parsers import PARSER_REGISTRY
from urllib.parse import urlparse
import re
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


class TxDocsMonitorConfigUpdate(BaseModel):
    target_id: int | None = Field(default=None, ge=1)
    spreadsheet_url: str = Field(min_length=1, max_length=1000)
    data_sheet_id: str = Field(min_length=1, max_length=100)
    parser_type: str = Field(min_length=1, max_length=50)
    header_row: int = Field(default=1, ge=1, le=100)
    interval_seconds: int = Field(default=600, ge=60, le=86400)
    client_id: str = Field(default="", max_length=200)
    access_token: str = Field(default="", max_length=10000)
    open_id: str = Field(default="", max_length=200)
    enabled: bool = False


def _txdocs_file_id(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() != "docs.qq.com":
        raise HTTPException(status_code=400, detail="腾讯文档链接必须是 https://docs.qq.com/sheet/... 地址")
    match = re.fullmatch(r"/sheet/([A-Za-z0-9_-]+)", parsed.path.rstrip("/"))
    if not match:
        raise HTTPException(status_code=400, detail="腾讯文档链接格式不受支持，请使用表格链接")
    return match.group(1)


def _txdocs_target_payload(target: dict, credentials: dict[str, str]) -> dict:
    has_credentials = bool(
        credentials.get("client_id")
        and credentials.get("access_token")
        and credentials.get("open_id")
    )
    ready = bool(
        target.get("enabled")
        and target.get("file_id")
        and target.get("sheet_id")
        and target.get("parser_type") in PARSER_REGISTRY
        and target.get("parser_type") != "default"
        and has_credentials
    )
    return {
        "id": int(target.get("id") or 1),
        "enabled": bool(target.get("enabled")),
        "configured": ready,
        "spreadsheet_url_configured": bool(target.get("spreadsheet_url")),
        "spreadsheet_url": target.get("spreadsheet_url", ""),
        "file_id": target.get("file_id", ""),
        "data_sheet_id": target.get("sheet_id", ""),
        "header_row": target.get("header_row", 1),
        "parser_type": target.get("parser_type", ""),
        "interval_seconds": target.get("interval_seconds", 600),
        "status": "已启用" if ready else ("已禁用" if not target.get("enabled") else "配置不完整"),
        "updated_at": target.get("updated_at").isoformat() if target.get("updated_at") else None,
    }


def _txdocs_config_payload(
    targets: list[dict] | dict, credentials: dict[str, str] | None = None
) -> dict:
    # Keep the helper compatible with the former singleton shape for callers
    # and tests during rolling upgrades.  The response is still normalized to
    # the multi-target shape and never exposes credential values.
    if isinstance(targets, dict):
        legacy = targets
        credentials = credentials or {
            "client_id": str(legacy.get("client_id") or ""),
            "access_token": str(legacy.get("access_token") or ""),
            "open_id": str(legacy.get("open_id") or ""),
        }
        targets = [legacy]
    credentials = credentials or {}
    target_payloads = [_txdocs_target_payload(target, credentials) for target in targets]
    configured = any(item["configured"] for item in target_payloads)
    environment_allowed = monitoring_environment_allowed()
    return {
        "environment_allowed": environment_allowed,
        "enabled": bool(environment_allowed and any(item["enabled"] for item in target_payloads)),
        "configured": configured,
        "targets": target_payloads,
        "client_id_configured": bool(credentials.get("client_id")),
        "access_token_configured": bool(credentials.get("access_token")),
        "open_id_configured": bool(credentials.get("open_id")),
        "status": (
            "当前环境不允许启用"
            if not environment_allowed
            else ("已启用" if configured else ("未配置" if not target_payloads else "配置不完整"))
        ),
    }


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


@router.get("/txdocs-monitor")
async def get_txdocs_monitor_overview(
    start_date: str = Query(..., description="yyyy-MM-dd"),
    end_date: str = Query(..., description="yyyy-MM-dd"),
    parser_type: str = Query("全链条"),
    user: dict = Depends(require_permission(ONLINE_SUMMARY_VIEW)),
    scope: ScopeMode = Query("permission"),
    community: str = Query("", max_length=100),
    conn=Depends(get_db),
):
    """读取独立的腾讯表只读监控聚合，不返回任何外部行正文。"""
    try:
        date.fromisoformat(start_date)
        date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期必须使用 yyyy-MM-dd") from exc
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    if parser_type not in IMPLEMENTED_SUBTYPES:
        raise HTTPException(status_code=400, detail="当前业务类型不支持在线汇总")
    try:
        async with conn.cursor() as cur:
            formal = await _requested_formal_communities(
                cur, user, scope, community
            )
            parser_types = (
                await _read_summary_types(cur)
                if parser_type == "总汇总表"
                else [parser_type]
            )
            configuration_state = await monitoring_configuration_state(cur)
        visible_communities = await _overview_community_names(conn, formal)
        return await get_txdocs_statistics_overview(
            start_date,
            end_date,
            parser_types,
            visible_communities,
            configuration_ready=configuration_state["configured"],
            monitoring_enabled=configuration_state["enabled"],
            environment_allowed=monitoring_environment_allowed(),
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/txdocs-monitor/config")
async def get_txdocs_monitor_config(_user: dict = Depends(require_super_admin), conn=Depends(get_db)):
    async with conn.cursor() as cur:
        await ensure_txdocs_monitor_config_schema(cur)
        targets = await _load_monitor_targets(cur)
        credentials = await _load_monitor_credentials(cur)
    return _txdocs_config_payload(targets, credentials)


@router.put("/txdocs-monitor/config")
async def update_txdocs_monitor_config(payload: TxDocsMonitorConfigUpdate, request: Request,
                                       user: dict = Depends(require_super_admin), conn=Depends(get_db)):
    # Keep non-Production environments completely free of Tencent monitoring
    # configuration and credentials.  Checking only ``payload.enabled`` would
    # still allow a disabled target to persist an access token or spreadsheet
    # URL in Dev, Staging, or Shadow.
    if not monitoring_environment_allowed():
        raise HTTPException(
            status_code=409,
            detail="腾讯只读监控配置只允许在 Production 环境维护；Development、Staging 和 Shadow 已由后端禁止。",
        )
    file_id = _txdocs_file_id(payload.spreadsheet_url)
    if payload.parser_type not in PARSER_REGISTRY or payload.parser_type == "default":
        raise HTTPException(status_code=400, detail="请选择受支持的只读监控业务类型")
    async with conn.cursor() as cur:
        await ensure_txdocs_monitor_config_schema(cur)
        current_credentials = await _load_monitor_credentials(cur)
        normalized_sheet_id = payload.data_sheet_id.strip()
        normalized_url = payload.spreadsheet_url.strip()
        # A target's natural key is the Tencent file, sub-sheet and parser.
        # Check it before changing the shared credentials so a mistaken edit
        # cannot partially save credentials and then fail on the unique key.
        await cur.execute(
            """SELECT id FROM _txdocs_monitor_target
               WHERE file_id=%s AND data_sheet_id=%s AND parser_type=%s
                 AND (%s IS NULL OR id<>%s)
               LIMIT 1""",
            (file_id, normalized_sheet_id, payload.parser_type,
             payload.target_id, payload.target_id),
        )
        duplicate = await cur.fetchone()
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail="相同腾讯表、数据子表和业务类型的监控目标已存在，请编辑已有目标",
            )
        client_id = payload.client_id.strip() or current_credentials["client_id"]
        open_id = payload.open_id.strip() or current_credentials["open_id"]
        access_token = (
            encrypt_secret(payload.access_token.strip())
            if payload.access_token.strip()
            else encrypt_secret(current_credentials["access_token"])
        )
        if payload.enabled and not (client_id and access_token and open_id):
            raise HTTPException(status_code=400, detail="启用监控前必须填写 client_id、access_token 和 open_id")
        await cur.execute(
            "SELECT id FROM _txdocs_monitor_connection WHERE id=1"
        )
        connection_exists = await cur.fetchone()
        if connection_exists:
            await cur.execute(
            """UPDATE _txdocs_monitor_connection
               SET client_id=%s, access_token=%s, open_id=%s, updated_by=%s
               WHERE id=1""",
            (client_id, access_token, open_id, int(user["id"])),
            )
        else:
            await cur.execute(
                """INSERT INTO _txdocs_monitor_connection
                   (id, client_id, access_token, open_id, updated_by)
                   VALUES (1,%s,%s,%s,%s)""",
                (client_id, access_token, open_id, int(user["id"])),
            )
        if payload.target_id is None:
            await cur.execute(
                """SELECT id FROM _txdocs_monitor_target
                   WHERE file_id=%s AND data_sheet_id=%s AND parser_type=%s
                   LIMIT 1""",
                (file_id, payload.data_sheet_id.strip(), payload.parser_type),
            )
            existing = await cur.fetchone()
            if existing:
                target_id = int(existing[0])
                await cur.execute(
                    """UPDATE _txdocs_monitor_target
                       SET enabled=%s, spreadsheet_url=%s, header_row=%s,
                           interval_seconds=%s, updated_by=%s
                       WHERE id=%s""",
                    (int(payload.enabled), normalized_url, payload.header_row,
                     payload.interval_seconds,
                     int(user["id"]), target_id),
                )
            else:
                await cur.execute(
                    """INSERT INTO _txdocs_monitor_target
                       (enabled, spreadsheet_url, file_id, data_sheet_id, header_row,
                        parser_type, interval_seconds, updated_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (int(payload.enabled), normalized_url, file_id,
                     normalized_sheet_id, payload.header_row,
                     payload.parser_type, payload.interval_seconds, int(user["id"])),
                )
                target_id = int(cur.lastrowid)
        else:
            await cur.execute(
                """UPDATE _txdocs_monitor_target
                   SET enabled=%s, spreadsheet_url=%s, file_id=%s, data_sheet_id=%s,
                       header_row=%s, parser_type=%s, interval_seconds=%s, updated_by=%s
                   WHERE id=%s""",
                (int(payload.enabled), normalized_url, file_id,
                 normalized_sheet_id, payload.header_row,
                 payload.parser_type, payload.interval_seconds, int(user["id"]),
                 payload.target_id),
            )
            if cur.rowcount != 1:
                raise HTTPException(status_code=404, detail="腾讯只读监控目标不存在")
            target_id = payload.target_id
    await record_admin_audit(user, "txdocs.monitor.config.update", target_type="txdocs_monitor_config",
                             target_name=f"腾讯只读监控目标:{target_id}", detail={"enabled": payload.enabled, "interval_seconds": payload.interval_seconds},
                             **request_audit_fields(request))
    async with conn.cursor() as cur:
        return _txdocs_config_payload(
            await _load_monitor_targets(cur), await _load_monitor_credentials(cur)
        )


@router.post("/txdocs-monitor/config/disable")
async def disable_txdocs_monitor_config(request: Request, user: dict = Depends(require_super_admin), conn=Depends(get_db)):
    async with conn.cursor() as cur:
        await ensure_txdocs_monitor_config_schema(cur)
        await cur.execute(
            "UPDATE _txdocs_monitor_target SET enabled=0, updated_by=%s",
            (int(user["id"]),),
        )
    await record_admin_audit(user, "txdocs.monitor.config.disable", target_type="txdocs_monitor_config", target_name="腾讯只读监控", detail={}, **request_audit_fields(request))
    return {"enabled": False, "message": "腾讯只读监控已禁用"}


@router.delete("/txdocs-monitor/config/{target_id}")
async def delete_txdocs_monitor_config(
    target_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await ensure_txdocs_monitor_config_schema(cur)
        await cur.execute("DELETE FROM _txdocs_monitor_target WHERE id=%s", (target_id,))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="腾讯只读监控目标不存在")
    await record_admin_audit(
        user,
        "txdocs.monitor.config.delete",
        target_type="txdocs_monitor_config",
        target_name=f"腾讯只读监控目标:{target_id}",
        detail={},
        **request_audit_fields(request),
    )
    return {"deleted": True, "target_id": target_id}


@router.post("/txdocs-monitor/run")
async def run_txdocs_monitor_now(request: Request, user: dict = Depends(require_super_admin)):
    runtime = await get_monitoring_runtime_state()
    if not runtime["environment_allowed"]:
        raise HTTPException(
            status_code=409,
            detail="腾讯只读监控只允许在 Production 环境启用；Development、Staging 和 Shadow 已由后端禁止。",
        )
    if not runtime["enabled_target_count"]:
        raise HTTPException(status_code=409, detail="请先在系统设置中启用至少一个腾讯只读监控目标。")
    if not runtime["configured"]:
        raise HTTPException(status_code=409, detail="已启用的腾讯只读监控目标或共享凭据配置不完整，请先补齐配置。")
    try:
        count = await run_txdocs_statistics_once()
    except Exception as exc:  # noqa: BLE001 - do not expose remote details
        raise HTTPException(status_code=502, detail="腾讯表读取失败，请查看监控状态后重试") from exc
    await record_admin_audit(user, "txdocs.monitor.run", target_type="txdocs_monitor_config", target_name="腾讯只读监控", detail={"successful_sources": count}, **request_audit_fields(request))
    return {"successful_sources": count, "message": "已完成一次只读读取"}


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
