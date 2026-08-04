"""组长、组员使用的手机任务首页和卡片式处理接口。"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import get_db
from deps import require_permission
from routers.query import (
    _enabled_spreadsheets,
    _managed_column_metadata,
    _source_ready,
    _writeback_enabled,
    update_source_fields,
)
from services.business_time import get_business_date
from services.data_scope import community_names_for_scopes
from services.online_edit_permissions import row_edit_capabilities
from services.online_source import json_value
from services.parsers import get_parser
from services.permissions import ONLINE_RAW_EDIT, ONLINE_RAW_VIEW
from services.report_overview import SUMMARY_TYPE, get_online_overview
from services.task_workflow import MOBILE_TASK_TYPES, TASK_WORKFLOWS


router = APIRouter(prefix="/api/mobile-tasks", tags=["手机任务工作台"])
FlowScope = Literal["mine", "community"]
TaskStatus = Literal["pending", "review", "completed", "all"]
ReviewStage = Literal["all", "waiting_analysis", "analyzed"]


class TaskBatchUpdate(BaseModel):
    changes: dict[str, str]
    expected_revision: int = Field(gt=0)


def _iso_utc(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    return str(value)


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
    if stage == "all" or workflow.valid_results:
        return "1=1"
    analysis = " OR ".join(
        f"{_json_field(field)}<>''" for field in workflow.analysis_fields
    ) or "0"
    unable = f"{_json_field(workflow.result_field)} LIKE '%%无法核实%%'"
    return (
        f"({unable} AND ({analysis}))"
        if stage == "analyzed"
        else f"({unable} AND NOT ({analysis}))"
    )


async def _flow_context(conn, user: dict) -> dict:
    name, community = require_flow_user(user)
    aliases = await community_names_for_scopes(conn, [community])
    return {
        "name": name,
        "position": str((user.get("member") or {}).get("position") or ""),
        "community": community,
        "community_values": aliases or [community],
    }


def _scope_where(
    context: dict,
    scope: FlowScope,
    *,
    alias: str = "projection",
) -> tuple[str, list[str]]:
    community_values = context["community_values"]
    placeholders = ", ".join(["%s"] * len(community_values))
    clause = f"{alias}.community IN ({placeholders})"
    params = list(community_values)
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


async def _assignee_options(cur, community: str) -> list[dict[str, str]]:
    await cur.execute(
        """
        SELECT DISTINCT member.name
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
          AND member.position='组员'
          AND member.status='在岗'
        ORDER BY member.name
        """,
        (community,),
    )
    return [
        {"id": str(row[0]), "text": str(row[0])}
        for row in await cur.fetchall()
        if row[0]
    ]


async def _validate_assignment(
    cur,
    context: dict,
    changes: dict[str, str],
) -> None:
    if "核查人" not in changes:
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
) -> dict:
    workflow = TASK_WORKFLOWS[parser_type]
    normalized = {key: str(value or "") for key, value in values.items()}
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
    }


def _source_in_community(parser, values: dict, community_values: list[str]) -> bool:
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
        personal = await _aggregate_live(cur, context, "mine")
        community = await _aggregate_live(cur, context, "community")
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
            inspector=context["name"],
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


@router.get("/{parser_type}")
async def list_mobile_tasks(
    parser_type: str,
    scope: FlowScope = Query("mine"),
    status: TaskStatus = Query("pending"),
    review_stage: ReviewStage = Query("all"),
    keyword: str | None = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    context = await _flow_context(conn, user)
    where, params = _scope_where(context, scope)
    where_parts = ["projection.parser_type=%s", where]
    query_params: list = [parser_type, *params]
    review_condition = _review_condition(parser_type)
    if status == "pending":
        where_parts.append("projection.task_state<>'completed'")
    elif status == "completed":
        where_parts.append("projection.task_state='completed'")
    elif status == "review":
        where_parts.append(review_condition)
        where_parts.append(_review_stage_condition(parser_type, review_stage))
    if keyword and keyword.strip():
        where_parts.append("projection.search_text LIKE %s")
        query_params.append(f"%{keyword.strip()}%")
    where_sql = " AND ".join(where_parts)

    async with conn.cursor() as cur:
        spreadsheets = await _enabled_spreadsheets(cur, parser_type)
        ready = await _source_ready(cur, spreadsheets)
        if not ready:
            return {
                "data": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "source_ready": False,
                "message": "来源定位尚未建立，请等待一次正常同步",
            }
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_source_projection AS projection "
            f"WHERE {where_sql}",
            query_params,
        )
        total = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            f"""
            SELECT projection.row_key, projection.values_json,
                   projection.source_count, projection.conflict,
                   projection.pending_state, projection.task_state
            FROM _online_source_projection AS projection
            WHERE {where_sql}
            ORDER BY {review_condition} DESC,
                     FIELD(projection.task_state, 'unchecked', 'checked', 'completed'),
                     projection.updated_at DESC, projection.row_key
            LIMIT %s OFFSET %s
            """,
            [*query_params, page_size, (page - 1) * page_size],
        )
        rows = await cur.fetchall()
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
            )
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "source_ready": True,
        "message": "",
    }


@router.get("/{parser_type}/{row_key}")
async def get_mobile_task_detail(
    parser_type: str,
    row_key: str,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    context = await _flow_context(conn, user)
    parser = get_parser(parser_type)
    placeholders = ", ".join(["%s"] * len(context["community_values"]))
    async with conn.cursor() as cur:
        if not await _source_ready(cur, await _enabled_spreadsheets(cur, parser_type)):
            raise HTTPException(409, "来源定位尚未建立，请等待一次正常同步")
        await cur.execute(
            f"""
            SELECT values_json, source_count, conflict, pending_state, task_state
            FROM _online_source_projection
            WHERE parser_type=%s AND row_key=%s
              AND community IN ({placeholders})
            """,
            (parser_type, row_key, *context["community_values"]),
        )
        parent_row = await cur.fetchone()
        if not parent_row:
            raise HTTPException(404, "任务不存在或不属于当前社区")
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
        assignee_options = (
            await _assignee_options(cur, context["community"])
            if context["position"] == "组长" else []
        )

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
            capabilities = await row_edit_capabilities(cur, user, parser, values)
            metadata = await _managed_column_metadata(
                cur,
                parser,
                json_value(raw_meta, {}),
                spreadsheet_id=int(spreadsheet_id),
                sheet_id=str(sheet_id),
            )
            if "核查人" in metadata and context["position"] == "组长":
                metadata["核查人"] = {
                    "type": "select",
                    "multiple": False,
                    "options": list(assignee_options),
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
            if context["position"] != "组长":
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

    parent_values = json_value(parent_row[0], {})
    workflow = TASK_WORKFLOWS[parser_type]
    return {
        "task": _task_record(
            parser_type,
            row_key,
            parent_values,
            len(sources),
            bool(parent_row[2]) and len(sources) > 1,
            str(parent_row[3] or "") == "pending",
            str(parent_row[4] or ""),
        ),
        "workflow": {
            "result_field": workflow.result_field,
            "phone_fields": list(workflow.phone_fields),
            "title_fields": list(workflow.title_fields),
            "address_fields": list(workflow.address_fields),
            "date_fields": list(workflow.date_fields),
            "secondary_fields": list(workflow.secondary_fields),
            "analysis_fields": list(workflow.analysis_fields),
            "columns": parser.COLUMNS,
        },
        "writeback_enabled": enabled,
        "sources": sources,
    }


@router.patch("/{parser_type}/source-rows/{source_id}")
async def update_mobile_task(
    parser_type: str,
    source_id: int,
    data: TaskBatchUpdate,
    request: Request,
    user: dict = Depends(require_permission(ONLINE_RAW_EDIT)),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
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
    )
