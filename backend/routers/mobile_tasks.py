"""组长、组员使用的手机任务首页和卡片式处理接口。"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import db_manager, get_db
from deps import get_current_user, require_permission
from routers.query import (
    _enabled_spreadsheets,
    _managed_column_metadata,
    _source_ready,
    _writeback_enabled,
    update_source_fields,
)
from routers.workflow import _can_view_ticket as workflow_ticket_access
from services.business_time import get_business_date
from services.data_scope import community_names_for_scopes
from services.online_edit_permissions import (
    effective_view_communities,
    inspector_option_context,
    row_edit_capabilities,
)
from services.online_source import json_value
from services.parsers import get_parser
from services.permissions import (
    ONLINE_RAW_EDIT,
    ONLINE_RAW_VIEW,
    ONLINE_TASK_MANAGE,
    WORKFLOW_ATTACHMENT_VIEW,
    has_permission,
)
from services.report_overview import SUMMARY_TYPE, get_online_overview
from services.task_workflow import MOBILE_TASK_TYPES, TASK_WORKFLOWS
from services.audit import record_admin_audit, request_audit_fields
from config import settings
from services.watch_matching import task_watch_payload


router = APIRouter(prefix="/api/mobile-tasks", tags=["手机任务工作台"])
FlowScope = Literal["mine", "community", "all"]
TaskStatus = Literal[
    "pending",
    "unchecked",
    "checked",
    "review",
    "completed",
    "all",
]
ReviewStage = Literal["all", "waiting_analysis", "analyzed"]
Priority = Literal[
    "all",
    "analyzed",
    "source_exception",
    "pending_sync",
    "ordinary",
    "waiting_analysis",
    "completed",
]
SortMode = Literal["priority", "updated_desc", "updated_asc"]
AssignmentMode = Literal["single", "balanced"]
EMPTY_FILTER_VALUE = "__empty__"


async def _task_photo_results(user: dict, parser_type: str, row_key: str) -> list[dict]:
    """返回与当前任务精确关联的已完成照片工单附件摘要。"""
    if (
        not settings.WORKFLOW_FEATURE_ENABLED
        or not has_permission(user, WORKFLOW_ATTACHMENT_VIEW)
    ):
        return []
    try:
        pool = db_manager.get_pool("workflow")
    except ValueError:
        return []
    results: list[dict] = []
    async with pool.acquire() as workflow_conn:
        async with workflow_conn.cursor() as cur:
            await cur.execute(
                "SELECT order_row.id, order_row.ticket_no "
                "FROM photo_request_details detail "
                "JOIN work_orders order_row ON order_row.id=detail.work_order_id "
                "WHERE detail.external_origin='platform_task' "
                "AND detail.source_parser_type=%s AND detail.source_row_key=%s "
                "AND detail.result_status='found' "
                "AND order_row.type_code='photo_request' "
                "AND order_row.status IN ('approved','completed') "
                "ORDER BY order_row.completed_at DESC, order_row.id DESC",
                (parser_type, row_key),
            )
            tickets = await cur.fetchall()
            for ticket_id, ticket_no in tickets:
                normalized_ticket_id = int(ticket_id)
                try:
                    await workflow_ticket_access(cur, normalized_ticket_id, user)
                except HTTPException as exc:
                    if exc.status_code in {403, 404}:
                        continue
                    raise
                await cur.execute(
                    "SELECT file_id, original_name, mime_type, size_bytes "
                    "FROM work_order_attachments "
                    "WHERE work_order_id=%s AND deleted_at IS NULL "
                    "AND mime_type LIKE 'image/%%' ORDER BY id DESC",
                    (normalized_ticket_id,),
                )
                attachments = [
                    {
                        "file_id": str(file_id),
                        "original_name": str(original_name or "照片"),
                        "mime_type": str(mime_type or "application/octet-stream"),
                        "size_bytes": int(size_bytes or 0),
                    }
                    for file_id, original_name, mime_type, size_bytes in await cur.fetchall()
                ]
                if attachments:
                    results.append({
                        "ticket_id": normalized_ticket_id,
                        "ticket_no": str(ticket_no or ""),
                        "attachments": attachments,
                    })
    return results
PRIORITY_KEYS = (
    "analyzed",
    "source_exception",
    "pending_sync",
    "ordinary",
    "waiting_analysis",
    "completed",
)
PRIORITY_LABELS = {
    "analyzed": "已研判",
    "source_exception": "来源异常",
    "pending_sync": "待同步",
    "ordinary": "普通待处理",
    "waiting_analysis": "等待研判",
    "completed": "已完成",
}
FLOW_TASK_ELEVATED_POSITIONS = {
    "片长",
    "基础管控",
    "中队长",
    "社区民警",
    "所队领导",
}


class TaskBatchUpdate(BaseModel):
    changes: dict[str, str]
    expected_revision: int = Field(gt=0)


class BulkAssignmentRequest(BaseModel):
    row_keys: list[str] = Field(min_length=1, max_length=100)
    inspector: str = Field(default="", max_length=100)
    mode: AssignmentMode = "single"


class TaskSearch(BaseModel):
    scope: FlowScope = "mine"
    status: TaskStatus = "pending"
    review_stage: ReviewStage = "all"
    communities: list[str] = Field(default_factory=list, max_length=50)
    inspectors: list[str] = Field(default_factory=list, max_length=50)
    watch_categories: list[int] = Field(default_factory=list, max_length=50)
    priority: Priority = "all"
    sort: SortMode = "priority"
    keyword: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


def _iso_utc(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    return str(value)


def _balanced_assignment_plan(
    row_keys: list[str],
    inspectors: list[str],
) -> tuple[dict[str, str], dict[str, int]]:
    """把已筛出的任务按连续区段尽量平均分给在岗组员。

    连续区段比轮流交错更适合任务列表的地址排序：相邻地址尽量仍由同一
    名组员处理，同时每人的任务数最多相差一条。真正的候选人和任务资格
    仍由批量接口在数据库事务前后重新校验。
    """
    keys = list(dict.fromkeys(str(key).strip() for key in row_keys if str(key).strip()))
    members = list(dict.fromkeys(str(name).strip() for name in inspectors if str(name).strip()))
    if not keys or not members:
        return {}, {name: 0 for name in members}
    base, remainder = divmod(len(keys), len(members))
    plan: dict[str, str] = {}
    counts = {name: 0 for name in members}
    cursor = 0
    for index, member in enumerate(members):
        size = base + (1 if index < remainder else 0)
        for key in keys[cursor:cursor + size]:
            plan[key] = member
        counts[member] = size
        cursor += size
    return plan, counts


def require_flow_user(user: dict) -> tuple[str, str]:
    member = user.get("member") or {}
    position = str(member.get("position") or "").strip()
    name = str(member.get("name") or "").strip()
    communities = [
        str(value).strip()
        for value in user.get("community_names") or []
        if str(value).strip()
    ]
    if position not in {"组员", "组长"}:
        raise HTTPException(403, "手机任务工作台目前仅向组员和组长开放")
    if not name:
        raise HTTPException(403, "当前账号尚未关联有效人员")
    if len(communities) != 1:
        raise HTTPException(403, "当前人员必须配置一个有效社区部门")
    return name, communities[0]


def is_flow_task_admin(user: dict) -> bool:
    group_codes = {
        str(group.get("code") or "")
        for group in user.get("permission_groups") or []
        if isinstance(group, dict)
    }
    legacy_group = str((user.get("permission_group") or {}).get("code") or "")
    return (
        str(user.get("role") or "") in {"admin", "super_admin"}
        or bool(group_codes & {"admin", "super_admin"})
        or legacy_group in {"admin", "super_admin"}
    )


def is_flow_task_elevated(user: dict) -> bool:
    position = str((user.get("member") or {}).get("position") or "").strip()
    return (
        is_flow_task_admin(user)
        or has_permission(user, ONLINE_TASK_MANAGE)
        or position in FLOW_TASK_ELEVATED_POSITIONS
    )


def _require_task_edit_user(user: dict) -> dict:
    if not (
        has_permission(user, ONLINE_RAW_EDIT)
        or has_permission(user, ONLINE_TASK_MANAGE)
    ):
        raise HTTPException(403, "当前权限组不能修改流口任务")
    if has_permission(user, ONLINE_RAW_EDIT):
        return user
    # online.task.manage 只在流口任务路由内借用现有逐行回写校验。
    # 不修改登录态中的权限，因此不会开放 Univer 或腾讯原始行管理。
    scoped_user = dict(user)
    scoped_user["permissions"] = list(dict.fromkeys([
        *(user.get("permissions") or []),
        ONLINE_RAW_EDIT,
    ]))
    return scoped_user


def _task_capability_user(user: dict) -> dict:
    if has_permission(user, ONLINE_TASK_MANAGE) and not has_permission(
        user, ONLINE_RAW_EDIT
    ):
        return _require_task_edit_user(user)
    return user


def _require_analysis_user(user: dict) -> dict:
    if not has_permission(user, ONLINE_TASK_MANAGE):
        raise HTTPException(403, "当前权限组不能处理网格核查研判")
    return _require_task_edit_user(user)


def _can_assign_tasks(context: dict) -> bool:
    return bool(context.get("admin_mode")) or context.get("position") == "组长"


def _json_field(field: str) -> str:
    safe = field.replace('"', '\\"')
    return (
        "TRIM(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(" 
        f"projection.values_json, '$.\"{safe}\"')), ''))"
    )


def _review_condition(parser_type: str) -> str:
    workflow = TASK_WORKFLOWS[parser_type]
    conditions = ["projection.conflict=1", "projection.source_count>1"]
    if not workflow.valid_results:
        conditions.append(
            f"({_json_field(workflow.result_field)} LIKE '%%无法核实%%')"
        )
    return "(" + " OR ".join(conditions) + ")"


def _review_stage_condition(parser_type: str, stage: ReviewStage) -> str:
    workflow = TASK_WORKFLOWS[parser_type]
    if stage == "all":
        return "1=1"
    if workflow.valid_results:
        return "1=0"
    analysis = " OR ".join(
        f"{_json_field(field)}<>''" for field in workflow.analysis_fields
    ) or "0"
    unable = f"{_json_field(workflow.result_field)} LIKE '%%无法核实%%'"
    return (
        f"({unable} AND ({analysis}))"
        if stage == "analyzed"
        else f"({unable} AND NOT ({analysis}))"
    )


def _priority_case(parser_type: str) -> str:
    workflow = TASK_WORKFLOWS[parser_type]
    source_exception = "projection.conflict=1 OR projection.source_count>1"
    if workflow.valid_results:
        analyzed = "0"
        waiting = "0"
    else:
        analysis = " OR ".join(
            f"{_json_field(field)}<>''" for field in workflow.analysis_fields
        ) or "0"
        unable = f"{_json_field(workflow.result_field)} LIKE '%%无法核实%%'"
        analyzed = f"({unable} AND ({analysis}))"
        waiting = f"({unable} AND NOT ({analysis}))"
    return (
        "CASE "
        "WHEN projection.task_state='completed' THEN 'completed' "
        f"WHEN {analyzed} THEN 'analyzed' "
        f"WHEN {source_exception} THEN 'source_exception' "
        "WHEN projection.pending_state='pending' THEN 'pending_sync' "
        f"WHEN {waiting} THEN 'waiting_analysis' "
        "ELSE 'ordinary' END"
    )


def _priority_order(parser_type: str) -> str:
    bucket = _priority_case(parser_type)
    return (
        f"CASE {bucket} "
        "WHEN 'analyzed' THEN 0 "
        "WHEN 'source_exception' THEN 1 "
        "WHEN 'pending_sync' THEN 2 "
        "WHEN 'ordinary' THEN 3 "
        "WHEN 'waiting_analysis' THEN 4 "
        "ELSE 5 END"
    )


def _address_order(parser_type: str) -> str:
    fields = TASK_WORKFLOWS[parser_type].address_fields
    candidates = ", ".join(f"NULLIF({_json_field(field)}, '')" for field in fields)
    raw_value = f"COALESCE({candidates}, '')" if candidates else "''"
    normalized = f"LOWER(REGEXP_REPLACE({raw_value}, '[[:space:]]+', ''))"
    return f"CASE WHEN {normalized}='' THEN 1 ELSE 0 END, {normalized}"


def _priority_bucket(
    parser_type: str,
    values: dict,
    source_count: int,
    conflict: bool,
    pending: bool,
    task_state_value: str,
) -> str:
    workflow = TASK_WORKFLOWS[parser_type]
    if task_state_value == "completed" or workflow.state(values) == "completed":
        return "completed"
    if workflow.review_stage(values) == "analyzed":
        return "analyzed"
    if conflict or source_count > 1:
        return "source_exception"
    if pending:
        return "pending_sync"
    if workflow.review_stage(values) == "waiting_analysis":
        return "waiting_analysis"
    return "ordinary"


def _multi_filter_condition(
    column: str,
    values: list[str],
) -> tuple[str, list[str]]:
    normalized = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    if not normalized:
        return "1=1", []
    include_empty = EMPTY_FILTER_VALUE in normalized
    non_empty = [value for value in normalized if value != EMPTY_FILTER_VALUE]
    predicates: list[str] = []
    params: list[str] = []
    if non_empty:
        placeholders = ", ".join(["%s"] * len(non_empty))
        predicates.append(f"projection.{column} IN ({placeholders})")
        params.extend(non_empty)
    if include_empty:
        predicates.append(f"TRIM(COALESCE(projection.{column}, ''))='' ")
    return "(" + " OR ".join(predicates) + ")", params


def _task_where(
    context: dict,
    parser_type: str,
    data: TaskSearch,
    *,
    include_priority: bool = True,
) -> tuple[str, list]:
    scope_where, scope_params = _scope_where(context, data.scope)
    where_parts = ["projection.parser_type=%s", scope_where]
    params: list = [parser_type, *scope_params]
    review_condition = _review_condition(parser_type)
    if data.status == "pending":
        where_parts.append("projection.task_state<>'completed'")
    elif data.status == "unchecked":
        where_parts.append("projection.task_state='unchecked'")
    elif data.status == "checked":
        where_parts.append("projection.task_state='checked'")
    elif data.status == "completed":
        where_parts.append("projection.task_state='completed'")
    elif data.status == "review":
        where_parts.append(review_condition)
    if data.review_stage != "all":
        where_parts.append(_review_stage_condition(parser_type, data.review_stage))
    community_condition, community_params = _multi_filter_condition(
        "community", data.communities
    )
    inspector_condition, inspector_params = _multi_filter_condition(
        "inspector", data.inspectors
    )
    where_parts.extend([community_condition, inspector_condition])
    params.extend(community_params)
    params.extend(inspector_params)
    keyword = data.keyword.strip()
    if keyword:
        where_parts.append("projection.search_text LIKE %s")
        params.append(f"%{keyword}%")
    if data.watch_categories:
        if not settings.REGISTRY_FEATURE_ENABLED:
            where_parts.append("1=0")
        else:
            placeholders = ",".join(["%s"] * len(data.watch_categories))
            registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
            where_parts.append(
                f"EXISTS (SELECT 1 FROM `{registry}`.online_task_watch_snapshots watch_snapshot "
                f"JOIN `{registry}`.watch_assignments watch_assignment "
                "ON watch_assignment.id=watch_snapshot.assignment_id "
                "WHERE watch_snapshot.parser_type=projection.parser_type "
                "AND watch_snapshot.row_key=projection.row_key "
                f"AND watch_assignment.category_id IN ({placeholders}))"
            )
            params.extend(data.watch_categories)
    if include_priority and data.priority != "all":
        where_parts.append(f"({_priority_case(parser_type)})=%s")
        params.append(data.priority)
    return " AND ".join(where_parts), params


async def _flow_context(conn, user: dict) -> dict:
    if is_flow_task_elevated(user):
        # online.task.manage 是流口任务工作台内的全所管理能力。
        # 它不会改变用户在 Univer、辖区档案等其他模块的数据范围。
        view_communities = (
            None
            if has_permission(user, ONLINE_TASK_MANAGE)
            else effective_view_communities(user)
        )
        community_values = (
            None
            if view_communities is None
            else await community_names_for_scopes(conn, view_communities)
        )
        member = user.get("member") or {}
        return {
            "name": str(
                member.get("name")
                or user.get("display_name")
                or user.get("username")
                or "管理员"
            ).strip(),
            "position": str(member.get("position") or "管理员").strip(),
            "community": "全所",
            "community_values": community_values,
            "admin_mode": True,
        }
    name, community = require_flow_user(user)
    aliases = await community_names_for_scopes(conn, [community])
    return {
        "name": name,
        "position": str((user.get("member") or {}).get("position") or ""),
        "community": community,
        "community_values": aliases or [community],
        "admin_mode": False,
    }


def _scope_where(
    context: dict,
    scope: FlowScope,
    *,
    alias: str = "projection",
) -> tuple[str, list[str]]:
    if scope == "all" and not context.get("admin_mode"):
        raise HTTPException(403, "组员和组长只能查看本人或本社区任务")
    community_values = context["community_values"]
    if community_values is None:
        clause = "1=1"
        params: list[str] = []
    elif community_values:
        placeholders = ", ".join(["%s"] * len(community_values))
        clause = f"{alias}.community IN ({placeholders})"
        params = list(community_values)
    else:
        clause = "1=0"
        params = []
    if scope == "mine":
        clause += f" AND LOWER(TRIM({alias}.inspector))=LOWER(TRIM(%s))"
        params.append(context["name"])
    return clause, params


async def _source_readiness(cur) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for parser_type in MOBILE_TASK_TYPES:
        spreadsheets = await _enabled_spreadsheets(cur, parser_type)
        result[parser_type] = await _source_ready(cur, spreadsheets)
    return result


async def _aggregate_live(cur, context: dict, scope: FlowScope) -> dict[str, dict]:
    where, params = _scope_where(context, scope)
    type_placeholders = ", ".join(["%s"] * len(MOBILE_TASK_TYPES))
    await cur.execute(
        f"""
        SELECT projection.parser_type, projection.task_state,
               COUNT(*),
               SUM(CASE WHEN projection.conflict=1
                              OR projection.source_count>1
                              OR (projection.parser_type IN (
                                      '全链条','出租房屋核查','寄递业'
                                  ) AND {_json_field('核查结果')}
                                      LIKE '%%无法核实%%')
                              OR (projection.parser_type='疑似返苏'
                                  AND {_json_field('核查反馈')}
                                      LIKE '%%无法核实%%')
                        THEN 1 ELSE 0 END)
        FROM _online_source_projection AS projection
        WHERE projection.parser_type IN ({type_placeholders})
          AND {where}
        GROUP BY projection.parser_type, projection.task_state
        """,
        (*MOBILE_TASK_TYPES, *params),
    )
    totals = {
        parser_type: {
            "parser_type": parser_type,
            "label": TASK_WORKFLOWS[parser_type].label,
            "pending": 0,
            "unchecked": 0,
            "checked": 0,
            "completed": 0,
            "review": 0,
        }
        for parser_type in MOBILE_TASK_TYPES
    }
    for parser_type, state, count, review in await cur.fetchall():
        item = totals[str(parser_type)]
        normalized_state = str(state or "unchecked")
        amount = int(count or 0)
        if normalized_state in {"unchecked", "checked", "completed"}:
            item[normalized_state] += amount
        if normalized_state != "completed":
            item["pending"] += amount
        item["review"] += int(review or 0)
    return totals


async def _validate_assignment(
    cur,
    context: dict,
    changes: dict[str, str],
) -> None:
    if "核查人" not in changes:
        return
    if context.get("admin_mode"):
        return
    if context["position"] != "组长":
        raise HTTPException(403, "只有组长可以在手机任务中转派任务")
    assignee = str(changes.get("核查人") or "").strip()
    if not assignee:
        raise HTTPException(400, "请选择要分配的组员")
    await cur.execute(
        """
        SELECT member.id
        FROM _grid_members AS member
        JOIN _grid_member_department_links AS link
          ON link.member_id=member.id
        JOIN _departments AS department
          ON department.id=link.department_id
         AND department.department_type='community'
         AND department.is_active=1
        JOIN _communities AS community
          ON community.id=department.community_id
        WHERE community.name=%s
          AND member.name=%s
          AND member.position='组员'
          AND member.status='在岗'
        LIMIT 1
        """,
        (context["community"], assignee),
    )
    if not await cur.fetchone():
        raise HTTPException(400, "只能分配给本社区在岗组员")


def _task_record(
    parser_type: str,
    row_key: str,
    values: dict,
    source_count: int,
    conflict: bool,
    pending: bool,
    task_state_value: str,
    watch: dict | None = None,
) -> dict:
    workflow = TASK_WORKFLOWS[parser_type]
    normalized = {key: str(value or "") for key, value in values.items()}
    watch = watch or {}
    return {
        "row_key": str(row_key),
        "parser_type": parser_type,
        "summary": workflow.summary(normalized),
        "community": str(normalized.get("社区") or normalized.get("下发社区") or ""),
        "inspector": str(normalized.get("核查人") or ""),
        "state": task_state_value or workflow.state(normalized),
        "needs_review": workflow.needs_review(
            normalized,
            source_count=int(source_count or 0),
            conflict=bool(conflict),
        ),
        "review_stage": workflow.review_stage(normalized),
        "source_count": int(source_count or 0),
        "conflict": bool(conflict),
        "pending_sync": bool(pending),
        "priority": _priority_bucket(
            parser_type,
            normalized,
            int(source_count or 0),
            bool(conflict),
            bool(pending),
            task_state_value,
        ),
        "watch_marks": list(watch.get("watch_marks") or []),
        "first_dispatch_at": _iso_utc(watch.get("first_dispatch_at")),
    }


def _source_in_community(
    parser,
    values: dict,
    community_values: list[str] | None,
) -> bool:
    if community_values is None:
        return True
    source_community = parser.community_value(values).strip()
    return bool(source_community and source_community in set(community_values))


@router.get("/home")
async def get_mobile_task_home(
    scope: FlowScope = Query("mine"),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    context = await _flow_context(conn, user)
    async with conn.cursor() as cur:
        business_date = await get_business_date(cur)
        readiness = await _source_readiness(cur)
        selected = await _aggregate_live(cur, context, scope)
        personal_scope: FlowScope = "all" if context["admin_mode"] else "mine"
        community_scope: FlowScope = "all" if context["admin_mode"] else "community"
        personal = await _aggregate_live(cur, context, personal_scope)
        community = await _aggregate_live(cur, context, community_scope)
        await cur.execute(
            "SELECT MAX(finished_at) FROM _sync_log "
            "WHERE status IN ('success', 'completed')"
        )
        sync_row = await cur.fetchone()

    date_text = business_date.isoformat()
    try:
        today = await get_online_overview(
            date_text,
            date_text,
            SUMMARY_TYPE,
            context["community_values"],
            inspector=None if context["admin_mode"] else context["name"],
            parser_types_override=list(MOBILE_TASK_TYPES),
        )
    except (RuntimeError, ValueError):
        today = {"exists": False}

    personal_totals = list(personal.values())
    community_totals = list(community.values())
    businesses = sorted(
        selected.values(),
        key=lambda item: (item["pending"] == 0, -item["pending"], item["label"]),
    )
    for item in businesses:
        item["source_ready"] = bool(readiness.get(item["parser_type"]))
    return {
        "business_date": date_text,
        "last_success_at": _iso_utc(sync_row[0]) if sync_row else None,
        "scope": scope,
        "admin_mode": context["admin_mode"],
        "person": {
            "name": context["name"],
            "position": context["position"],
            "community": context["community"],
        },
        "personal": {
            "pending": sum(item["pending"] for item in personal_totals),
            "new_today": int(today.get("new_tasks") or 0) if today.get("exists") else None,
            "carryover_today": int(today.get("carryover_tasks") or 0) if today.get("exists") else None,
            "completed_today": int(today.get("completed_tasks") or 0) if today.get("exists") else None,
        },
        "community": {
            "pending": sum(item["pending"] for item in community_totals),
        },
        "daily_snapshot_available": bool(today.get("exists")),
        "businesses": businesses,
    }


def _empty_facets() -> dict:
    return {
        "total": 0,
        "priority_counts": {key: 0 for key in PRIORITY_KEYS},
        "status_counts": {key: 0 for key in ("unchecked", "checked", "completed")},
    }


async def _task_facets(cur, parser_type: str, where_sql: str, params: list) -> dict:
    facets = _empty_facets()
    bucket_sql = _priority_case(parser_type)
    await cur.execute(
        f"""
        SELECT bucket, COUNT(*)
        FROM (
            SELECT {bucket_sql} AS bucket
            FROM _online_source_projection AS projection
            WHERE {where_sql}
        ) AS priority_rows
        GROUP BY bucket
        """,
        params,
    )
    for bucket, count in await cur.fetchall():
        if str(bucket) in facets["priority_counts"]:
            facets["priority_counts"][str(bucket)] = int(count or 0)
    await cur.execute(
        f"""
        SELECT projection.task_state, COUNT(*)
        FROM _online_source_projection AS projection
        WHERE {where_sql}
        GROUP BY projection.task_state
        """,
        params,
    )
    for state, count in await cur.fetchall():
        if str(state) in facets["status_counts"]:
            facets["status_counts"][str(state)] = int(count or 0)
    facets["total"] = sum(facets["priority_counts"].values())
    return facets


async def _task_filter_options(
    cur,
    parser_type: str,
    context: dict,
    scope: FlowScope,
    user: dict,
    communities: list[str] | None = None,
    review_stage: ReviewStage = "all",
) -> dict:
    capability_user = _task_capability_user(user)
    scope_where, scope_params = _scope_where(context, scope)
    where_sql = f"projection.parser_type=%s AND {scope_where}"
    params = [parser_type, *scope_params]
    if review_stage != "all":
        where_sql = f"{where_sql} AND {_review_stage_condition(parser_type, review_stage)}"
    inspector_where = where_sql
    inspector_params = list(params)
    community_condition, community_params = _multi_filter_condition(
        "community", communities or []
    )
    if community_condition != "1=1":
        inspector_where = f"{inspector_where} AND {community_condition}"
        inspector_params.extend(community_params)
    result = {"communities": [], "inspectors": [], "watch_categories": []}
    for column, key, empty_label in (
        ("community", "communities", "社区未填写"),
        ("inspector", "inspectors", "未分配核查人"),
    ):
        option_where = inspector_where if column == "inspector" else where_sql
        option_params = inspector_params if column == "inspector" else params
        await cur.execute(
            f"""
            SELECT projection.{column}, COUNT(*)
            FROM _online_source_projection AS projection
            WHERE {option_where}
            GROUP BY projection.{column}
            ORDER BY CASE WHEN TRIM(COALESCE(projection.{column}, ''))='' THEN 1 ELSE 0 END,
                     projection.{column}
            """,
            option_params,
        )
        for value, count in await cur.fetchall():
            normalized = str(value or "").strip()
            result[key].append({
                "value": normalized or EMPTY_FILTER_VALUE,
                "label": normalized or empty_label,
                "count": int(count or 0),
            })
    if settings.REGISTRY_FEATURE_ENABLED:
        registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
        await cur.execute(
            f"""
            SELECT category.id, category.name, category.color, category.alert_level,
                   COUNT(DISTINCT projection.row_key)
            FROM _online_source_projection projection
            JOIN `{registry}`.online_task_watch_snapshots snapshot
              ON snapshot.parser_type=projection.parser_type
             AND snapshot.row_key=projection.row_key
            JOIN `{registry}`.watch_assignments assignment
              ON assignment.id=snapshot.assignment_id
            JOIN `{registry}`.watch_categories category
              ON category.id=assignment.category_id
            WHERE {where_sql}
            GROUP BY category.id, category.name, category.color, category.alert_level
            ORDER BY category.sort_order, category.id
            """,
            params,
        )
        result["watch_categories"] = [
            {"value": int(row[0]), "label": str(row[1]), "color": str(row[2]),
             "alert_level": str(row[3]), "count": int(row[4] or 0)}
            for row in await cur.fetchall()
        ]
    assignment_enabled = (
        _can_assign_tasks(context)
        and (
            has_permission(user, ONLINE_RAW_EDIT)
            or has_permission(user, ONLINE_TASK_MANAGE)
        )
    )
    assignment_context = (
        await inspector_option_context(cur, capability_user, assignment_only=True)
        if assignment_enabled
        else {
            "community_aliases": {},
            "inspectors_by_community": {},
        }
    )
    result["assignment"] = {
        "enabled": assignment_enabled,
        "community_aliases": assignment_context["community_aliases"],
        "inspectors_by_community": assignment_context["inspectors_by_community"],
    }
    return result


async def _list_mobile_tasks_data(
    parser_type: str,
    data: TaskSearch,
    user: dict,
    conn,
) -> dict:
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    context = await _flow_context(conn, user)
    where_sql, query_params = _task_where(context, parser_type, data)
    facet_data = data.model_copy(update={
        "status": "all",
        "review_stage": "all",
        "priority": "all",
    })
    base_where, base_params = _task_where(
        context,
        parser_type,
        facet_data,
        include_priority=False,
    )
    async with conn.cursor() as cur:
        spreadsheets = await _enabled_spreadsheets(cur, parser_type)
        ready = await _source_ready(cur, spreadsheets)
        if not ready:
            return {
                "data": [],
                "total": 0,
                "page": data.page,
                "page_size": data.page_size,
                "source_ready": False,
                "message": "来源定位尚未建立，请等待一次正常同步",
                "facets": _empty_facets(),
                "priority_labels": PRIORITY_LABELS,
                "filters": {
                    "scope": data.scope,
                    "status": data.status,
                    "review_stage": data.review_stage,
                    "communities": data.communities,
                    "inspectors": data.inspectors,
                    "watch_categories": data.watch_categories,
                    "priority": data.priority,
                    "sort": data.sort,
                    "keyword_present": bool(data.keyword.strip()),
                },
            }
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_source_projection AS projection "
            f"WHERE {where_sql}",
            query_params,
        )
        total = int((await cur.fetchone())[0] or 0)
        facets = await _task_facets(cur, parser_type, base_where, base_params)
        completed_last = (
            "CASE WHEN projection.task_state='completed' THEN 1 ELSE 0 END"
        )
        if data.sort == "updated_asc":
            order_sql = (
                f"{completed_last}, projection.updated_at ASC, projection.row_key"
            )
        elif data.sort == "updated_desc":
            order_sql = (
                f"{completed_last}, projection.updated_at DESC, projection.row_key"
            )
        else:
            order_sql = (
                f"{_priority_order(parser_type)}, "
                f"{_address_order(parser_type)}, "
                "projection.updated_at DESC, projection.row_key"
            )
        await cur.execute(
            f"""
            SELECT projection.row_key, projection.values_json,
                   projection.source_count, projection.conflict,
                   projection.pending_state, projection.task_state
            FROM _online_source_projection AS projection
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT %s OFFSET %s
            """,
            [*query_params, data.page_size, (data.page - 1) * data.page_size],
        )
        rows = await cur.fetchall()
        watch_by_row = await task_watch_payload(
            cur,
            parser_type,
            [str(row[0]) for row in rows],
        ) if settings.REGISTRY_FEATURE_ENABLED else {}
    return {
        "data": [
            _task_record(
                parser_type,
                str(row[0]),
                json_value(row[1], {}),
                int(row[2] or 0),
                bool(row[3]),
                str(row[4] or "") == "pending",
                str(row[5] or ""),
                watch_by_row.get(str(row[0])),
            )
            for row in rows
        ],
        "total": total,
        "page": data.page,
        "page_size": data.page_size,
        "source_ready": True,
        "message": "",
        "facets": facets,
        "priority_labels": PRIORITY_LABELS,
        "filters": {
            "scope": data.scope,
            "status": data.status,
            "review_stage": data.review_stage,
            "communities": data.communities,
            "inspectors": data.inspectors,
            "watch_categories": data.watch_categories,
            "priority": data.priority,
            "sort": data.sort,
            "keyword_present": bool(data.keyword.strip()),
        },
    }


@router.get("/{parser_type}/filter-options")
async def get_mobile_task_filter_options(
    parser_type: str,
    scope: FlowScope = Query("mine"),
    community: list[str] = Query(default=[]),
    review_stage: ReviewStage = Query("all"),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    context = await _flow_context(conn, user)
    async with conn.cursor() as cur:
        spreadsheets = await _enabled_spreadsheets(cur, parser_type)
        if not await _source_ready(cur, spreadsheets):
            return {
                "source_ready": False,
                "communities": [],
                "inspectors": [],
                "watch_categories": [],
                "assignment": {
                    "enabled": False,
                    "community_aliases": {},
                    "inspectors_by_community": {},
                },
            }
        options = await _task_filter_options(
            cur,
            parser_type,
            context,
            scope,
            user,
            communities=community,
            review_stage=review_stage,
        )
    return {"source_ready": True, **options}


@router.post("/{parser_type}/search")
async def search_mobile_tasks(
    parser_type: str,
    data: TaskSearch,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    return await _list_mobile_tasks_data(parser_type, data, user, conn)


@router.get("/{parser_type}")
async def list_mobile_tasks(
    parser_type: str,
    scope: FlowScope = Query("mine"),
    status: TaskStatus = Query("pending"),
    review_stage: ReviewStage = Query("all"),
    community: list[str] = Query(default=[]),
    inspector: list[str] = Query(default=[]),
    watch_category: list[int] = Query(default=[]),
    priority: Priority = Query("all"),
    sort: SortMode = Query("priority"),
    keyword: str | None = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    return await _list_mobile_tasks_data(
        parser_type,
        TaskSearch(
            scope=scope,
            status=status,
            review_stage=review_stage,
            communities=community,
            inspectors=inspector,
            watch_categories=watch_category,
            priority=priority,
            sort=sort,
            keyword=keyword or "",
            page=page,
            page_size=page_size,
        ),
        user,
        conn,
    )


async def _mobile_task_detail_data(
    parser_type: str,
    row_key: str,
    user: dict,
    conn,
    *,
    analysis_mode: bool = False,
) -> dict:
    if analysis_mode:
        user = _require_analysis_user(user)
    capability_user = _task_capability_user(user)
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    context = await _flow_context(conn, user)
    parser = get_parser(parser_type)
    detail_scope: FlowScope = "all" if context["admin_mode"] else "community"
    scope_where, scope_params = _scope_where(context, detail_scope)
    async with conn.cursor() as cur:
        if not await _source_ready(cur, await _enabled_spreadsheets(cur, parser_type)):
            raise HTTPException(409, "来源定位尚未建立，请等待一次正常同步")
        await cur.execute(
            f"""
            SELECT values_json, source_count, conflict, pending_state, task_state
            FROM _online_source_projection AS projection
            WHERE projection.parser_type=%s AND projection.row_key=%s
              AND {scope_where}
            """,
            (parser_type, row_key, *scope_params),
        )
        parent_row = await cur.fetchone()
        if not parent_row:
            raise HTTPException(404, "任务不存在或不属于当前社区")
        parent_values = json_value(parent_row[0], {})
        if analysis_mode and TASK_WORKFLOWS[parser_type].review_stage(
            parent_values
        ) not in {"waiting_analysis", "analyzed"}:
            raise HTTPException(404, "该任务当前不属于网格核查研判范围")
        await cur.execute(
            """
            SELECT id, physical_row, values_json, cell_meta_json,
                   revision, row_hash, spreadsheet_id, sheet_id
            FROM _online_source_rows
            WHERE parser_type=%s AND row_key=%s
            ORDER BY spreadsheet_id, physical_row
            """,
            (parser_type, row_key),
        )
        raw_sources = await cur.fetchall()
        enabled = await _writeback_enabled(cur)
        assignment_context = (
            await inspector_option_context(cur, capability_user, assignment_only=True)
            if _can_assign_tasks(context) and (
                has_permission(user, ONLINE_RAW_EDIT)
                or has_permission(user, ONLINE_TASK_MANAGE)
            )
            else None
        )
        watch_by_row = await task_watch_payload(cur, parser_type, [row_key]) \
            if settings.REGISTRY_FEATURE_ENABLED else {}

        sources = []
        for (
            source_id,
            physical_row,
            raw_values,
            raw_meta,
            revision,
            row_hash,
            spreadsheet_id,
            sheet_id,
        ) in raw_sources:
            values = json_value(raw_values, {})
            # 同一业务主键偶尔会跨社区重复。父投影可以用于定位任务，
            # 但详情绝不能因此暴露其他社区的腾讯原始行。
            if not _source_in_community(
                parser, values, context["community_values"]
            ):
                continue
            capabilities = await row_edit_capabilities(
                cur, capability_user, parser, values
            )
            metadata = await _managed_column_metadata(
                cur,
                parser,
                json_value(raw_meta, {}),
                spreadsheet_id=int(spreadsheet_id),
                sheet_id=str(sheet_id),
            )
            raw_community = parser.community_value(values)
            formal_community = (
                (assignment_context or {}).get("community_aliases", {})
                .get(str(raw_community or "").strip(), "")
            )
            assignee_options = list(
                (assignment_context or {}).get("inspectors_by_community", {})
                .get(formal_community, [])
            )
            if "核查人" in metadata and assignee_options:
                metadata["核查人"] = {
                    "type": "select",
                    "multiple": False,
                    "options": [
                        {"id": name, "text": name}
                        for name in assignee_options
                    ],
                }
            source_task = _task_record(
                parser_type,
                row_key,
                values,
                1,
                False,
                str(parent_row[3] or "") == "pending",
                TASK_WORKFLOWS[parser_type].state(values),
            )
            editable_fields = (
                list(capabilities["editable_fields"])
                if enabled else []
            )
            if context["position"] != "组长" and not context["admin_mode"]:
                editable_fields = [
                    field for field in editable_fields if field != "核查人"
                ]
            # 二次反馈是否真正允许保存，仍由批量校验根据提交后的主结果
            # 决定；这里先把字段交给手机表单，才能在同一次提交中展开。
            if enabled and capabilities["can_edit"]:
                editable_fields.extend(
                    field for field in TASK_WORKFLOWS[parser_type].secondary_fields
                    if field in parser.COLUMNS
                )
            analysis_fields = set(TASK_WORKFLOWS[parser_type].analysis_fields)
            if analysis_mode:
                editable_fields = [
                    field for field in analysis_fields
                    if field in parser.COLUMNS
                ] if source_task["review_stage"] in {
                    "waiting_analysis", "analyzed"
                } else []
            else:
                editable_fields = [
                    field for field in editable_fields
                    if field not in analysis_fields
                ]
            sources.append({
                "id": int(source_id),
                "physical_row": int(physical_row),
                "values": {
                    column: str(values.get(column, "") or "")
                    for column in parser.COLUMNS
                },
                "cell_meta": metadata,
                "revision": int(revision),
                "row_hash": str(row_hash),
                "editable_fields": list(dict.fromkeys(editable_fields)),
                "state": source_task["state"],
                "needs_review": source_task["needs_review"],
                "review_stage": source_task["review_stage"],
            })

        if not sources:
            raise HTTPException(404, "任务不存在或不属于当前社区")

    workflow = TASK_WORKFLOWS[parser_type]
    photo_requests = await _task_photo_results(user, parser_type, row_key)
    return {
        "task": _task_record(
            parser_type,
            row_key,
            parent_values,
            len(sources),
            bool(parent_row[2]) and len(sources) > 1,
            str(parent_row[3] or "") == "pending",
            str(parent_row[4] or ""),
            watch_by_row.get(row_key),
        ),
        "workflow": {
            "label": workflow.label,
            "result_field": workflow.result_field,
            "phone_fields": list(workflow.phone_fields),
            "title_fields": list(workflow.title_fields),
            "address_fields": list(workflow.address_fields),
            "date_fields": list(workflow.date_fields),
            "identity_fields": list(workflow.identity_fields),
            "source_fields": list(workflow.source_fields),
            "secondary_fields": list(workflow.secondary_fields),
            "extra_edit_fields": list(getattr(parser, "MOBILE_EDITABLE_FIELDS", ())),
            "analysis_fields": list(workflow.analysis_fields),
            "columns": parser.COLUMNS,
        },
        "writeback_enabled": enabled,
        "analysis_mode": analysis_mode,
        "photo_requests": photo_requests,
        "sources": sources,
    }


@router.get("/analysis/{parser_type}/{row_key}")
async def get_mobile_task_analysis_detail(
    parser_type: str,
    row_key: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    return await _mobile_task_detail_data(
        parser_type, row_key, user, conn, analysis_mode=True
    )


@router.patch("/analysis/{parser_type}/source-rows/{source_id}")
async def update_mobile_task_analysis(
    parser_type: str,
    source_id: int,
    data: TaskBatchUpdate,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    scoped_user = _require_analysis_user(user)
    parser = get_parser(parser_type)
    analysis_fields = {
        field for field in TASK_WORKFLOWS[parser_type].analysis_fields
        if field in parser.COLUMNS
    }
    if not analysis_fields:
        raise HTTPException(400, "该业务没有可填写的研判字段")
    if not data.changes or any(field not in analysis_fields for field in data.changes):
        raise HTTPException(400, "研判入口只能修改研判字段")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT values_json FROM _online_source_rows "
            "WHERE id=%s AND parser_type=%s",
            (source_id, parser_type),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "腾讯来源行不存在")
        values = json_value(row[0], {})
        if TASK_WORKFLOWS[parser_type].review_stage(values) not in {
            "waiting_analysis", "analyzed"
        }:
            raise HTTPException(409, "该任务当前不属于网格核查研判范围")
    def validate_analysis_source(current: dict) -> None:
        if TASK_WORKFLOWS[parser_type].review_stage(current) not in {
            "waiting_analysis", "analyzed"
        }:
            raise HTTPException(409, "该任务当前不属于网格核查研判范围")

    return await update_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes=data.changes,
        expected_revision=data.expected_revision,
        request=request,
        user=scoped_user,
        conn=conn,
        explicit_text_edit=True,
        allowed_columns=analysis_fields,
        current_values_validator=validate_analysis_source,
        redact_audit_values=True,
    )


@router.get("/{parser_type}/{row_key}")
async def get_mobile_task_detail(
    parser_type: str,
    row_key: str,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    return await _mobile_task_detail_data(parser_type, row_key, user, conn)


@router.patch("/{parser_type}/source-rows/{source_id}")
async def update_mobile_task(
    parser_type: str,
    source_id: int,
    data: TaskBatchUpdate,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    user = _require_task_edit_user(user)
    analysis_fields = set(TASK_WORKFLOWS[parser_type].analysis_fields)
    if any(field in analysis_fields for field in data.changes):
        raise HTTPException(403, "请从研判页面修改研判内容")
    context = await _flow_context(conn, user)
    async with conn.cursor() as cur:
        await _validate_assignment(cur, context, data.changes)
    return await update_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes=data.changes,
        expected_revision=data.expected_revision,
        request=request,
        user=user,
        conn=conn,
        explicit_text_edit=True,
    )


@router.post("/{parser_type}/bulk-assign")
async def bulk_assign_mobile_tasks(
    parser_type: str,
    data: BulkAssignmentRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Manually assign selected unassigned tasks to one or all local members.

    Tencent writeback is inherently row/version based, so each physical source
    row is still revalidated and written through the existing safe path.  The
    endpoint only provides the batch selection and a compact result summary;
    it never bypasses the per-row permission, revision, hash, or option checks.
    """
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以批量分配核查人")

    row_keys = list(dict.fromkeys(str(value).strip() for value in data.row_keys if str(value).strip()))
    inspector = str(data.inspector or "").strip()
    if not row_keys:
        raise HTTPException(400, "请选择任务")
    if data.mode == "single" and not inspector:
        raise HTTPException(400, "请选择在岗组员")

    projection_by_key: dict[str, dict] = {}
    source_rows_by_key: dict[str, list[tuple[int, int]]] = {}
    skipped: list[dict[str, str]] = []
    async with conn.cursor() as cur:
        if not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        scope_where, scope_params = _scope_where(
            context,
            "all" if context.get("admin_mode") else "community",
        )
        key_placeholders = ", ".join(["%s"] * len(row_keys))
        await cur.execute(
            f"""
            SELECT projection.row_key, projection.community, projection.inspector,
                   projection.task_state, projection.conflict, projection.source_count
            FROM _online_source_projection AS projection
            WHERE projection.parser_type=%s
              AND projection.row_key IN ({key_placeholders})
              AND {scope_where}
            """,
            [parser_type, *row_keys, *scope_params],
        )
        for row in await cur.fetchall():
            projection_by_key[str(row[0])] = {
                "community": str(row[1] or "").strip(),
                "inspector": str(row[2] or "").strip(),
                "state": str(row[3] or "").strip(),
                "conflict": bool(row[4]),
                "source_count": int(row[5] or 0),
            }
        missing_scope = [key for key in row_keys if key not in projection_by_key]
        if missing_scope:
            raise HTTPException(403, "部分任务不在当前账号可操作范围内，请刷新后重试")

        assignment_context = await inspector_option_context(
            cur,
            user,
            assignment_only=True,
        )
        community_aliases = assignment_context["community_aliases"]
        inspectors_by_community = assignment_context["inspectors_by_community"]
        formal_communities = {
            community_aliases.get(item["community"], "")
            for item in projection_by_key.values()
        }
        if not formal_communities or "" in formal_communities:
            raise HTTPException(403, "部分任务社区不在当前账号可分配范围内")
        if len(formal_communities) != 1:
            raise HTTPException(400, "批量分配必须一次只选择同一社区的任务")
        formal_community = next(iter(formal_communities))
        available_inspectors = list(
            inspectors_by_community.get(formal_community) or []
        )
        if not available_inspectors:
            raise HTTPException(400, "该社区当前没有在岗组员可分配")
        if data.mode == "single" and inspector not in available_inspectors:
            raise HTTPException(400, "只能分配给任务所属社区的在岗组员")

        await cur.execute(
            f"""
            SELECT id, row_key, revision
            FROM _online_source_rows
            WHERE parser_type=%s AND row_key IN ({key_placeholders})
            ORDER BY row_key, id
            """,
            [parser_type, *row_keys],
        )
        for source_id, row_key, revision in await cur.fetchall():
            source_rows_by_key.setdefault(str(row_key), []).append((int(source_id), int(revision)))

    eligible_keys: list[str] = []
    for row_key in row_keys:
        item = projection_by_key[row_key]
        if item["inspector"]:
            skipped.append({"row_key": row_key, "reason": "已有核查人"})
            continue
        if item["state"] == "completed":
            skipped.append({"row_key": row_key, "reason": "任务已完成"})
            continue
        if item["conflict"]:
            skipped.append({"row_key": row_key, "reason": "来源存在冲突"})
            continue
        if not source_rows_by_key.get(row_key):
            skipped.append({"row_key": row_key, "reason": "找不到来源行"})
            continue
        eligible_keys.append(row_key)

    if data.mode == "balanced":
        assignment_plan, assignment_counts = _balanced_assignment_plan(
            eligible_keys,
            available_inspectors,
        )
    else:
        assignment_plan = {row_key: inspector for row_key in eligible_keys}
        assignment_counts = {
            inspector: len(eligible_keys),
        }

    update_count = 0
    failed_count = 0
    successful_assignment_counts = {name: 0 for name in assignment_counts}
    for row_key in eligible_keys:
        assigned_inspector = assignment_plan.get(row_key, "")
        if not assigned_inspector:
            skipped.append({"row_key": row_key, "reason": "没有生成分配方案"})
            continue
        sources = source_rows_by_key.get(row_key) or []
        if not sources:
            skipped.append({"row_key": row_key, "reason": "找不到来源行"})
            continue
        task_ok = True
        for source_id, revision in sources:
            try:
                await update_source_fields(
                    parser_type=parser_type,
                    source_id=source_id,
                    changes={"核查人": assigned_inspector},
                    expected_revision=revision,
                    request=request,
                    user=user,
                    conn=conn,
                    explicit_text_edit=True,
                )
            except HTTPException as exc:
                task_ok = False
                failed_count += 1
                reason = {
                    400: "数据校验未通过",
                    403: "没有该任务的编辑权限",
                    409: "任务已变化，请刷新后重试",
                    502: "腾讯回写校验失败",
                }.get(exc.status_code, "保存失败")
                skipped.append({"row_key": row_key, "reason": reason})
                break
        if task_ok:
            update_count += 1
            successful_assignment_counts[assigned_inspector] = (
                successful_assignment_counts.get(assigned_inspector, 0) + 1
            )

    await record_admin_audit(
        user,
        "mobile_tasks.bulk_assign",
        target_type="mobile_task",
        target_name=parser_type,
        detail={
            "task_count": len(row_keys),
            "updated_count": update_count,
            "skipped_count": len(skipped),
            "failed_count": failed_count,
            "inspector": inspector if data.mode == "single" else "",
            "allocation_mode": data.mode,
            "assignment_counts": successful_assignment_counts,
        },
        **request_audit_fields(request),
    )
    return {
        "updated": update_count,
        "skipped": len(skipped),
        "failed": failed_count,
        "details": skipped,
        "inspector": inspector if data.mode == "single" else "",
        "mode": data.mode,
        "assignment_counts": successful_assignment_counts,
    }
