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
    queue_source_fields,
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
from services.online_local_writeback import (
    launch_local_change_processing,
    load_local_changes,
    local_sync_state,
    overlay_local_values,
    resolve_source_conflict,
)
from services.parsers import get_parser
from services.permissions import (
    ONLINE_RAW_EDIT,
    ONLINE_RAW_VIEW,
    ONLINE_TASK_MANAGE,
    QMF_REGISTRATION_EXECUTE,
    WORKFLOW_ATTACHMENT_VIEW,
    has_permission,
)
from services.report_overview import SUMMARY_TYPE, get_online_overview
from services.qmf_registration import (
    MODEL_THREE_PARSER,
    normalize_qmf_result,
    preview_capability,
    registration_capability,
)
from services.qmf_config import load_qmf_config
from services.qmf_runs import WRITE_STEP_KEYS, parse_steps, utc_text
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
QmfFeedbackState = Literal[
    "not_scanned",
    "stale",
    "pending",
    "completed_match",
    "completed_mismatch",
    "not_found",
    "error",
]
EMPTY_FILTER_VALUE = "__empty__"
MAX_BULK_ASSIGNMENT_TASKS = 2000
MAX_BULK_ASSIGNMENT_CHUNK = 20


async def _qmf_registration_state(
    conn,
    *,
    parser_type: str,
    sources: list[dict],
    user: dict,
) -> tuple[dict | None, dict | None]:
    """Return a private resumable run plus the public successful summary.

    Only users with the全民防 registration permission receive prepared,
    executing, failed or uncertain details. Other task viewers can only see
    that the external feedback succeeded and when it completed.
    """
    if parser_type != MODEL_THREE_PARSER or not sources:
        return None, None
    source_ids = [int(item["id"]) for item in sources]
    placeholders = ",".join(["%s"] * len(source_ids))
    latest_run = None
    async with conn.cursor() as cur:
        if has_permission(user, QMF_REGISTRATION_EXECUTE):
            await cur.execute(
                "UPDATE _qmf_registration_runs "
                "SET status='expired', result_code='prepare_expired' "
                f"WHERE parser_type=%s AND source_id IN ({placeholders}) "
                "AND requested_by=%s AND status='prepared' "
                "AND expires_at<=UTC_TIMESTAMP()",
                (parser_type, *source_ids, int(user["id"])),
            )
            await cur.execute(
                "SELECT id, source_id, expected_revision, status, steps_json, "
                "result_code, tencent_marker_status, tencent_marker_error, "
                "prepared_at, expires_at, execution_started_at, completed_at, "
                "created_at, updated_at "
                f"FROM _qmf_registration_runs WHERE parser_type=%s "
                f"AND source_id IN ({placeholders}) AND requested_by=%s "
                "ORDER BY id DESC LIMIT 1",
                (parser_type, *source_ids, int(user["id"])),
            )
            row = await cur.fetchone()
            if row:
                status = str(row[3] or "")
                marker_status = str(row[6] or "not_started")
                steps = parse_steps(row[4])
                write_progress = any(
                    item["key"] in WRITE_STEP_KEYS
                    and item["status"] in {"sending", "succeeded", "uncertain"}
                    for item in steps
                )
                latest_run = {
                    "id": int(row[0]),
                    "parser_type": parser_type,
                    "source_id": int(row[1]),
                    "expected_revision": int(row[2]),
                    "status": status,
                    "steps": steps,
                    "result_code": str(row[5] or ""),
                    "photo": {"sha256": "", "mime_type": "", "size_bytes": 0},
                    "tencent_marker_status": marker_status,
                    "tencent_marker_error": str(row[7] or ""),
                    "prepared_at": utc_text(row[8]),
                    "expires_at": utc_text(row[9]),
                    "execution_started_at": utc_text(row[10]),
                    "completed_at": utc_text(row[11]),
                    "created_at": utc_text(row[12]),
                    "updated_at": utc_text(row[13]),
                    "can_execute": status == "prepared",
                    "can_reprepare": status in {"failed", "uncertain"}
                    and not write_progress,
                    "can_retry_marker": status == "succeeded" and marker_status in {
                        "not_started", "pending", "conflict", "failed",
                    },
                }
        await cur.execute(
            "SELECT id, completed_at, tencent_marker_status "
            f"FROM _qmf_registration_runs WHERE parser_type=%s "
            f"AND source_id IN ({placeholders}) AND status='succeeded' "
            "ORDER BY id DESC LIMIT 1",
            (parser_type, *source_ids),
        )
        succeeded = await cur.fetchone()
    feedback = None
    if succeeded:
        feedback = {
            "run_id": int(succeeded[0]),
            "status": "succeeded",
            "completed_at": utc_text(succeeded[1]),
            "tencent_marker_status": str(succeeded[2] or "not_started"),
        }
    return latest_run, feedback


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


async def _task_photo_fetched_rows(
    user: dict,
    parser_type: str,
    row_keys: list[str],
) -> set[str]:
    """Return task row keys with an accessible, completed photo result.

    The list view asks for several rows at once so photo status does not cause
    one workflow-database query per task card.  Ticket visibility still goes
    through the same access check used by the detail endpoint.
    """
    if (
        not row_keys
        or not settings.WORKFLOW_FEATURE_ENABLED
        or not has_permission(user, WORKFLOW_ATTACHMENT_VIEW)
    ):
        return set()
    try:
        pool = db_manager.get_pool("workflow")
    except ValueError:
        return set()
    placeholders = ",".join(["%s"] * len(row_keys))
    fetched: set[str] = set()
    async with pool.acquire() as workflow_conn:
        async with workflow_conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT detail.source_row_key, order_row.id "
                "FROM photo_request_details detail "
                "JOIN work_orders order_row ON order_row.id=detail.work_order_id "
                "JOIN work_order_attachments attachment "
                "ON attachment.work_order_id=order_row.id "
                "AND attachment.deleted_at IS NULL "
                "AND attachment.mime_type LIKE 'image/%%' "
                "WHERE detail.external_origin='platform_task' "
                "AND detail.source_parser_type=%s "
                f"AND detail.source_row_key IN ({placeholders}) "
                "AND detail.result_status='found' "
                "AND order_row.type_code='photo_request' "
                "AND order_row.status IN ('approved','completed')",
                [parser_type, *row_keys],
            )
            candidates = await cur.fetchall()
            for source_row_key, ticket_id in candidates:
                normalized_ticket_id = int(ticket_id)
                try:
                    await workflow_ticket_access(cur, normalized_ticket_id, user)
                except HTTPException as exc:
                    if exc.status_code in {403, 404}:
                        continue
                    raise
                fetched.add(str(source_row_key))
    return fetched
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
    base_values: dict[str, str] = Field(default_factory=dict)
    expected_revision: int = Field(gt=0)


class SyncConflictResolution(BaseModel):
    choice: Literal["platform", "tencent"]
    fields: list[str] = Field(min_length=1, max_length=5)


class BulkAssignmentRequest(BaseModel):
    row_keys: list[str] = Field(min_length=1, max_length=MAX_BULK_ASSIGNMENT_CHUNK)
    inspector: str = Field(default="", max_length=100)
    mode: AssignmentMode = "single"
    balanced_offset: int = Field(default=0, ge=0)
    balanced_total: int = Field(default=0, ge=0, le=MAX_BULK_ASSIGNMENT_TASKS)


class TaskSearch(BaseModel):
    scope: FlowScope = "mine"
    status: TaskStatus = "pending"
    review_stage: ReviewStage = "all"
    communities: list[str] = Field(default_factory=list, max_length=50)
    inspectors: list[str] = Field(default_factory=list, max_length=50)
    watch_categories: list[int] = Field(default_factory=list, max_length=50)
    qmf_feedback_states: list[QmfFeedbackState] = Field(
        default_factory=list, max_length=10
    )
    priority: Priority = "all"
    sort: SortMode = "priority"
    keyword: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class AnalysisTaskSearch(BaseModel):
    parser_types: list[str] = Field(min_length=1, max_length=20)
    scope: FlowScope = "all"
    review_stage: ReviewStage = "all"
    communities: list[str] = Field(default_factory=list, max_length=50)
    inspectors: list[str] = Field(default_factory=list, max_length=50)
    watch_categories: list[int] = Field(default_factory=list, max_length=50)
    sort: SortMode = "priority"
    keyword: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class InlineEditorRequest(BaseModel):
    row_keys: list[str] = Field(min_length=1, max_length=50)


def _iso_utc(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    return str(value)


def _balanced_assignment_plan(
    row_keys: list[str],
    inspectors: list[str],
    *,
    total_count: int | None = None,
    start_index: int = 0,
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
    total = total_count if total_count is not None else len(keys)
    if total < len(keys) or start_index < 0 or start_index + len(keys) > total:
        return {}, {name: 0 for name in members}
    base, remainder = divmod(total, len(members))
    member_ranges: list[tuple[int, int, str]] = []
    range_start = 0
    for index, member in enumerate(members):
        size = base + (1 if index < remainder else 0)
        member_ranges.append((range_start, range_start + size, member))
        range_start += size
    plan: dict[str, str] = {}
    counts = {name: 0 for name in members}
    for local_index, key in enumerate(keys):
        global_index = start_index + local_index
        member = next(
            name
            for lower, upper, name in member_ranges
            if lower <= global_index < upper
        )
        plan[key] = member
        counts[member] += 1
    return plan, counts


def _bulk_assignment_result(
    *,
    updated: int,
    skipped: list[dict[str, str]],
    failures: list[dict[str, str]],
    inspector: str,
    mode: AssignmentMode,
    assignment_counts: dict[str, int],
) -> dict:
    """Build mutually exclusive assignment outcome counts and details."""
    return {
        "updated": updated,
        "skipped": len(skipped),
        "failed": len(failures),
        "details": skipped,
        "failed_details": failures,
        "inspector": inspector if mode == "single" else "",
        "mode": mode,
        "assignment_counts": assignment_counts,
    }


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
        "WHEN projection.pending_state IN ('pending','retry','conflict') THEN 'pending_sync' "
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


def _qmf_feedback_state_sql() -> str:
    raw_result = _json_field("核查结果")
    current_result = (
        f"CASE {raw_result} "
        "WHEN '离吴' THEN '离开不返吴' "
        "WHEN '近期反吴' THEN '近期返吴' "
        f"ELSE {raw_result} END"
    )
    return f"""
        COALESCE((
            SELECT CASE
                WHEN snapshot.error_code<>'' THEN 'error'
                WHEN snapshot.platform_result<>{current_result}
                  OR snapshot.last_scanned_at<DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
                  OR source.id IS NULL
                  OR source.revision<>snapshot.source_revision
                  OR source.row_hash<>snapshot.source_row_hash
                THEN 'stale'
                WHEN snapshot.feedback_state='pending' THEN 'pending'
                WHEN snapshot.feedback_state='completed_match' THEN 'completed_match'
                WHEN snapshot.feedback_state='completed_mismatch' THEN 'completed_mismatch'
                WHEN snapshot.feedback_state='not_found' THEN 'not_found'
                ELSE 'error'
            END
            FROM _qmf_status_snapshots AS snapshot
            LEFT JOIN _online_source_rows AS source
              ON source.id=snapshot.source_id
             AND source.parser_type=snapshot.parser_type
             AND source.row_key=snapshot.row_key
            WHERE snapshot.parser_type=projection.parser_type
              AND snapshot.row_key=projection.row_key
            LIMIT 1
        ), 'not_scanned')
    """.strip()


async def _qmf_status_by_rows(
    cur,
    parser_type: str,
    rows: list[tuple],
) -> dict[str, dict]:
    if parser_type != MODEL_THREE_PARSER or not rows:
        return {}
    row_values = {
        str(row[0]): json_value(row[1], {})
        for row in rows
    }
    keys = list(row_values)
    placeholders = ",".join(["%s"] * len(keys))
    await cur.execute(
        f"""
        SELECT snapshot.row_key,snapshot.source_id,snapshot.source_revision,
               snapshot.source_row_hash,snapshot.platform_result,
               snapshot.feedback_state,snapshot.feedback_result,
               snapshot.checked_at,snapshot.origin,snapshot.error_code,
               snapshot.last_scanned_at,
               source.id,source.revision,source.row_hash
        FROM _qmf_status_snapshots AS snapshot
        LEFT JOIN _online_source_rows AS source
          ON source.id=snapshot.source_id
         AND source.parser_type=snapshot.parser_type
         AND source.row_key=snapshot.row_key
        WHERE snapshot.parser_type=%s
          AND snapshot.row_key IN ({placeholders})
        """,
        (parser_type, *keys),
    )
    result: dict[str, dict] = {}
    now = datetime.utcnow()
    for row in await cur.fetchall():
        row_key = str(row[0])
        platform_result = normalize_qmf_result(
            row_values.get(row_key, {}).get("核查结果")
        )
        stale = (
            str(row[4] or "") != platform_result
            or row[11] is None
            or int(row[12] or 0) != int(row[2] or 0)
            or str(row[13] or "") != str(row[3] or "")
            or not row[10]
            or (now - row[10]).total_seconds() > 7 * 24 * 60 * 60
        )
        raw_state = str(row[5] or "")
        state = (
            "error" if str(row[9] or "")
            else "stale" if stale
            else raw_state if raw_state in {
                "pending", "completed_match", "completed_mismatch", "not_found"
            }
            else "error"
        )
        result[row_key] = {
            "state": state,
            "platform_result": str(row[4] or ""),
            "feedback_result": str(row[6] or ""),
            "checked_at": str(row[7] or ""),
            "origin": str(row[8] or ""),
            "error_code": str(row[9] or ""),
            "last_scanned_at": _iso_utc(row[10]),
        }
    for row_key, values in row_values.items():
        result.setdefault(row_key, {
            "state": "not_scanned",
            "platform_result": normalize_qmf_result(values.get("核查结果")),
            "feedback_result": "",
            "checked_at": "",
            "origin": "",
            "error_code": "",
            "last_scanned_at": None,
        })
    return result


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
    if data.qmf_feedback_states:
        if parser_type != MODEL_THREE_PARSER:
            where_parts.append("1=0")
        else:
            placeholders = ",".join(["%s"] * len(data.qmf_feedback_states))
            where_parts.append(
                f"({_qmf_feedback_state_sql()}) IN ({placeholders})"
            )
            params.extend(data.qmf_feedback_states)
    if include_priority and data.priority != "all":
        where_parts.append(f"({_priority_case(parser_type)})=%s")
        params.append(data.priority)
    return " AND ".join(where_parts), params


def _analysis_stage_condition(
    parser_types: list[str],
    stage: ReviewStage,
) -> tuple[str, list[str]]:
    conditions: list[str] = []
    params: list[str] = []
    stages = (stage,) if stage != "all" else ("waiting_analysis", "analyzed")
    for parser_type in parser_types:
        stage_conditions = [
            _review_stage_condition(parser_type, current_stage)
            for current_stage in stages
        ]
        conditions.append(
            "(projection.parser_type=%s AND ("
            + " OR ".join(stage_conditions)
            + "))"
        )
        params.append(parser_type)
    return "(" + " OR ".join(conditions) + ")", params


def _analysis_task_where(
    context: dict,
    data: AnalysisTaskSearch,
    *,
    review_stage: ReviewStage | None = None,
) -> tuple[str, list]:
    parser_types = list(dict.fromkeys(data.parser_types))
    type_placeholders = ",".join(["%s"] * len(parser_types))
    scope_where, scope_params = _scope_where(context, "all")
    where_parts = [
        f"projection.parser_type IN ({type_placeholders})",
        scope_where,
    ]
    params: list = [*parser_types, *scope_params]
    stage_sql, stage_params = _analysis_stage_condition(
        parser_types,
        review_stage if review_stage is not None else data.review_stage,
    )
    where_parts.append(stage_sql)
    params.extend(stage_params)
    community_condition, community_params = _multi_filter_condition(
        "community", data.communities
    )
    inspector_condition, inspector_params = _multi_filter_condition(
        "inspector", data.inspectors
    )
    where_parts.extend([community_condition, inspector_condition])
    params.extend(community_params)
    params.extend(inspector_params)
    if data.keyword.strip():
        where_parts.append("projection.search_text LIKE %s")
        params.append(f"%{data.keyword.strip()}%")
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
    return " AND ".join(where_parts), params


def _analysis_order(data: AnalysisTaskSearch) -> str:
    parser_types = list(dict.fromkeys(data.parser_types))
    analyzed: list[str] = []
    waiting: list[str] = []
    for parser_type in parser_types:
        analyzed.append(
            f"(projection.parser_type='{parser_type}' AND "
            f"{_review_stage_condition(parser_type, 'analyzed')})"
        )
        waiting.append(
            f"(projection.parser_type='{parser_type}' AND "
            f"{_review_stage_condition(parser_type, 'waiting_analysis')})"
        )
    stage_order = (
        "CASE WHEN " + " OR ".join(analyzed) + " THEN 0 "
        "WHEN " + " OR ".join(waiting) + " THEN 1 ELSE 2 END"
    )
    if data.sort == "updated_asc":
        return f"{stage_order}, projection.updated_at ASC, projection.row_key"
    if data.sort == "updated_desc":
        return f"{stage_order}, projection.updated_at DESC, projection.row_key"
    return f"{stage_order}, projection.updated_at DESC, projection.row_key"


async def _analysis_filter_options(
    cur,
    context: dict,
    user: dict,
    data: AnalysisTaskSearch,
) -> dict:
    option_data = data.model_copy(update={
        "communities": [],
    })
    base_where, base_params = _analysis_task_where(
        context,
        option_data,
        review_stage=data.review_stage,
    )
    community_condition, community_params = _multi_filter_condition(
        "community", data.communities
    )
    inspector_where = f"{base_where} AND {community_condition}"
    inspector_params = [*base_params, *community_params]
    result = {"communities": [], "inspectors": [], "watch_categories": []}
    for column, key, empty_label, option_where, option_params in (
        ("community", "communities", "社区未填写", base_where, base_params),
        ("inspector", "inspectors", "未分配核查人", inspector_where, inspector_params),
    ):
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
            WHERE {base_where}
            GROUP BY category.id, category.name, category.color, category.alert_level
            ORDER BY category.sort_order, category.id
            """,
            base_params,
        )
        result["watch_categories"] = [
            {"value": int(row[0]), "label": str(row[1]), "color": str(row[2]),
             "alert_level": str(row[3]), "count": int(row[4] or 0)}
            for row in await cur.fetchall()
        ]
    assignment_context = await inspector_option_context(
        cur,
        _task_capability_user(user),
        assignment_only=True,
    )
    result["assignment"] = {
        "enabled": False,
        "community_aliases": assignment_context["community_aliases"],
        "inspectors_by_community": assignment_context["inspectors_by_community"],
    }
    return result


async def _list_analysis_tasks_data(
    data: AnalysisTaskSearch,
    user: dict,
    conn,
) -> dict:
    parser_types = list(dict.fromkeys(data.parser_types))
    if not parser_types or any(parser_type not in TASK_WORKFLOWS for parser_type in parser_types):
        raise HTTPException(400, "存在尚未接入研判工作台的业务表")
    context = await _flow_context(conn, user)
    where_sql, query_params = _analysis_task_where(context, data)
    base_data = data.model_copy(update={"review_stage": "all"})
    base_where, base_params = _analysis_task_where(
        context,
        base_data,
        review_stage="all",
    )
    async with conn.cursor() as cur:
        ready_values = []
        for parser_type in parser_types:
            ready_values.append(await _source_ready(cur, await _enabled_spreadsheets(cur, parser_type)))
        if not all(ready_values):
            return {
                "data": [], "total": 0, "page": data.page, "page_size": data.page_size,
                "source_ready": False, "message": "部分业务表来源尚未建立，请等待一次正常同步",
                "facets": _empty_facets(), "priority_labels": PRIORITY_LABELS,
                "filters": {"parser_types": parser_types, "scope": data.scope,
                    "review_stage": data.review_stage, "communities": data.communities,
                    "inspectors": data.inspectors, "watch_categories": data.watch_categories,
                    "sort": data.sort, "keyword_present": bool(data.keyword.strip())},
            }
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_source_projection AS projection WHERE {where_sql}",
            query_params,
        )
        total = int((await cur.fetchone())[0] or 0)
        facets = _empty_facets()
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_source_projection AS projection WHERE {base_where}",
            base_params,
        )
        facets["total"] = int((await cur.fetchone())[0] or 0)
        for stage, key in (("waiting_analysis", "waiting_analysis"), ("analyzed", "analyzed")):
            stage_where, stage_params = _analysis_task_where(context, base_data, review_stage=stage)
            await cur.execute(
                f"SELECT COUNT(*) FROM _online_source_projection AS projection WHERE {stage_where}",
                stage_params,
            )
            facets["priority_counts"][key] = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            f"""
            SELECT projection.task_state, COUNT(*)
            FROM _online_source_projection AS projection
            WHERE {base_where}
            GROUP BY projection.task_state
            """,
            base_params,
        )
        for state, count in await cur.fetchall():
            if str(state) in facets["status_counts"]:
                facets["status_counts"][str(state)] = int(count or 0)
        await cur.execute(
            f"""
            SELECT projection.parser_type, projection.row_key, projection.values_json,
                   projection.source_count, projection.conflict,
                   projection.pending_state, projection.task_state
            FROM _online_source_projection AS projection
            WHERE {where_sql}
            ORDER BY {_analysis_order(data)}
            LIMIT %s OFFSET %s
            """,
            [*query_params, data.page_size, (data.page - 1) * data.page_size],
        )
        rows = await cur.fetchall()
        watch_by_parser: dict[str, dict[str, dict]] = {}
        for parser_type in parser_types:
            keys = [str(row[1]) for row in rows if str(row[0]) == parser_type]
            watch_by_parser[parser_type] = (
                await task_watch_payload(cur, parser_type, keys)
                if settings.REGISTRY_FEATURE_ENABLED and keys else {}
            )
    photo_fetched: dict[str, set[str]] = {}
    for parser_type in parser_types:
        keys = [str(row[1]) for row in rows if str(row[0]) == parser_type]
        photo_fetched[parser_type] = await _task_photo_fetched_rows(user, parser_type, keys)
    return {
        "data": [
            _task_record(
                str(row[0]), str(row[1]), json_value(row[2], {}), int(row[3] or 0),
                bool(row[4]), bool(str(row[5] or "")), str(row[6] or ""),
                watch_by_parser.get(str(row[0]), {}).get(str(row[1])),
                str(row[1]) in photo_fetched.get(str(row[0]), set()),
                str(row[5] or ""),
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
        "filters": {"parser_types": parser_types, "scope": data.scope,
            "review_stage": data.review_stage, "communities": data.communities,
            "inspectors": data.inspectors, "watch_categories": data.watch_categories,
            "sort": data.sort, "keyword_present": bool(data.keyword.strip())},
    }


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
          AND member.position IN ('组长', '组员')
          AND member.status='在岗'
        LIMIT 1
        """,
        (context["community"], assignee),
    )
    if not await cur.fetchone():
        raise HTTPException(400, "只能分配给本社区在岗组长或组员")


def _task_record(
    parser_type: str,
    row_key: str,
    values: dict,
    source_count: int,
    conflict: bool,
    pending: bool,
    task_state_value: str,
    watch: dict | None = None,
    photo_fetched: bool = False,
    sync_state: str = "",
    qmf_status: dict | None = None,
) -> dict:
    workflow = TASK_WORKFLOWS[parser_type]
    normalized = {key: str(value or "") for key, value in values.items()}
    watch = watch or {}
    return {
        "task_key": f"{parser_type}:{row_key}",
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
        "sync_state": sync_state or ("pending" if pending else ""),
        "photo_fetched": bool(photo_fetched),
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
        "qmf_status": qmf_status,
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
        "qmf_feedback_counts": {
            key: 0 for key in (
                "not_scanned", "stale", "pending", "completed_match",
                "completed_mismatch", "not_found", "error",
            )
        },
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
    if parser_type == MODEL_THREE_PARSER:
        await cur.execute(
            f"""
            SELECT qmf_state,COUNT(*)
            FROM (
                SELECT {_qmf_feedback_state_sql()} AS qmf_state
                FROM _online_source_projection AS projection
                WHERE {where_sql} AND projection.task_state='completed'
            ) AS qmf_rows
            GROUP BY qmf_state
            """,
            params,
        )
        for state, count in await cur.fetchall():
            normalized = str(state or "error")
            if normalized in facets["qmf_feedback_counts"]:
                facets["qmf_feedback_counts"][normalized] = int(count or 0)
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
        "qmf_feedback_states": [],
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
                    "qmf_feedback_states": data.qmf_feedback_states,
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
        qmf_by_row = await _qmf_status_by_rows(cur, parser_type, rows)
        watch_by_row = await task_watch_payload(
            cur,
            parser_type,
            [str(row[0]) for row in rows],
        ) if settings.REGISTRY_FEATURE_ENABLED else {}
    photo_fetched_rows = await _task_photo_fetched_rows(
        user,
        parser_type,
        [str(row[0]) for row in rows],
    )
    return {
        "data": [
            _task_record(
                parser_type,
                str(row[0]),
                json_value(row[1], {}),
                int(row[2] or 0),
                bool(row[3]),
                bool(str(row[4] or "")),
                str(row[5] or ""),
                watch_by_row.get(str(row[0])),
                str(row[0]) in photo_fetched_rows,
                str(row[4] or ""),
                qmf_status=(
                    qmf_by_row.get(str(row[0]))
                    if str(row[5] or "") == "completed"
                    else None
                ),
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
            "qmf_feedback_states": data.qmf_feedback_states,
            "priority": data.priority,
            "sort": data.sort,
            "keyword_present": bool(data.keyword.strip()),
        },
    }


@router.get("/analysis/filter-options")
async def get_mobile_task_analysis_filter_options(
    parser_type: list[str] = Query(default=[]),
    community: list[str] = Query(default=[]),
    review_stage: ReviewStage = Query("all"),
    user: dict = Depends(require_permission(ONLINE_TASK_MANAGE)),
    conn=Depends(get_db),
):
    parser_types = list(dict.fromkeys(parser_type or list(MOBILE_TASK_TYPES)))
    if any(value not in TASK_WORKFLOWS for value in parser_types):
        raise HTTPException(400, "存在尚未接入研判工作台的业务表")
    context = await _flow_context(conn, user)
    data = AnalysisTaskSearch(
        parser_types=parser_types,
        review_stage=review_stage,
        communities=community,
    )
    async with conn.cursor() as cur:
        ready = all([
            await _source_ready(cur, await _enabled_spreadsheets(cur, value))
            for value in parser_types
        ])
        if not ready:
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
        options = await _analysis_filter_options(cur, context, user, data)
    return {"source_ready": True, **options}


@router.post("/analysis/search")
async def search_mobile_task_analysis(
    data: AnalysisTaskSearch,
    user: dict = Depends(require_permission(ONLINE_TASK_MANAGE)),
    conn=Depends(get_db),
):
    return await _list_analysis_tasks_data(data, user, conn)


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


@router.post("/{parser_type}/assignment-selection")
async def select_mobile_tasks_for_assignment(
    parser_type: str,
    data: TaskSearch,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Resolve every currently eligible task for the complete filter result."""
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以批量分配核查人")

    where_sql, query_params = _task_where(context, parser_type, data)
    async with conn.cursor() as cur:
        if not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        await cur.execute(
            f"""
            SELECT projection.row_key, projection.community
            FROM _online_source_projection AS projection
            WHERE {where_sql}
              AND TRIM(COALESCE(projection.inspector, ''))=''
              AND projection.task_state<>'completed'
              AND projection.conflict=0
              AND EXISTS (
                  SELECT 1 FROM _online_source_rows AS source_row
                  WHERE source_row.parser_type=projection.parser_type
                    AND source_row.row_key=projection.row_key
              )
            ORDER BY {_address_order(parser_type)}, projection.row_key
            LIMIT %s
            """,
            [*query_params, MAX_BULK_ASSIGNMENT_TASKS + 1],
        )
        rows = await cur.fetchall()
        if len(rows) > MAX_BULK_ASSIGNMENT_TASKS:
            raise HTTPException(
                409,
                f"当前筛选可分配任务超过 {MAX_BULK_ASSIGNMENT_TASKS} 条，请继续缩小筛选范围",
            )
        assignment_context = await inspector_option_context(
            cur,
            user,
            assignment_only=True,
        )

    aliases = assignment_context["community_aliases"]
    formal_communities = {
        aliases.get(str(community or "").strip(), "")
        for _, community in rows
    }
    if "" in formal_communities:
        raise HTTPException(403, "部分任务社区不在当前账号可分配范围内")
    if len(formal_communities) > 1:
        raise HTTPException(400, "请先筛选到一个社区，再全选当前筛选结果")
    formal_community = next(iter(formal_communities), "")
    if formal_community and not assignment_context["inspectors_by_community"].get(
        formal_community
    ):
        raise HTTPException(400, "该社区当前没有在岗组员可分配")
    return {
        "row_keys": [str(row_key) for row_key, _ in rows],
        "total": len(rows),
        "community": formal_community,
    }


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
    include_photo_requests: bool = True,
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
        local_changes = await load_local_changes(
            cur, [int(row[0]) for row in raw_sources]
        )
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
        qmf_by_row = await _qmf_status_by_rows(
            cur,
            parser_type,
            [(row_key, parent_row[0])],
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
            source_changes = local_changes.get(int(source_id), [])
            values = overlay_local_values(
                json_value(raw_values, {}), source_changes
            )
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
                bool(str(parent_row[3] or "")),
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
                "source_available": int(physical_row) > 0,
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
                "sync_state": local_sync_state(source_changes),
                "sync_fields": [
                    {
                        "field": item["field_name"],
                        "platform_value": item["local_value"],
                        "tencent_value": item["remote_value"],
                        "status": item["status"],
                        "error_code": item["error_code"],
                    }
                    for item in source_changes
                ],
            })

        if not sources:
            raise HTTPException(404, "任务不存在或不属于当前社区")

    workflow = TASK_WORKFLOWS[parser_type]
    photo_requests = (
        await _task_photo_results(user, parser_type, row_key)
        if include_photo_requests else []
    )
    qmf_config = await load_qmf_config(conn)
    qmf_allowed = has_permission(user, QMF_REGISTRATION_EXECUTE)
    qmf_preview = preview_capability(
        allowed=qmf_allowed,
        parser_type=parser_type,
        source_count=int(parent_row[1] or 0),
        conflict=bool(parent_row[2]),
        values=sources[0]["values"] if len(sources) == 1 else None,
        config=qmf_config,
    )
    qmf_registration = registration_capability(
        allowed=qmf_allowed,
        parser_type=parser_type,
        source_count=int(parent_row[1] or 0),
        conflict=bool(parent_row[2]),
        values=sources[0]["values"] if len(sources) == 1 else None,
        config=qmf_config,
    )
    latest_qmf_run, qmf_feedback = await _qmf_registration_state(
        conn,
        parser_type=parser_type,
        sources=sources,
        user=user,
    )
    qmf_registration["latest_run"] = latest_qmf_run
    return {
        "task": _task_record(
            parser_type,
            row_key,
            parent_values,
            len(sources),
            bool(parent_row[2]) and len(sources) > 1,
            bool(str(parent_row[3] or "")),
            str(parent_row[4] or ""),
            watch_by_row.get(row_key),
            False,
            str(parent_row[3] or ""),
            qmf_status=(
                qmf_by_row.get(row_key)
                if str(parent_row[4] or "") == "completed"
                else None
            ),
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
        "qmf_preview": qmf_preview,
        "qmf_registration": qmf_registration,
        "qmf_feedback": qmf_feedback,
        "qmf_status": qmf_by_row.get(row_key),
        "sources": sources,
    }


async def _mobile_task_inline_editors_data(
    parser_type: str,
    row_keys: list[str],
    user: dict,
    conn,
    *,
    analysis_mode: bool = False,
) -> dict:
    """Prepare the current page's editors in one browser request.

    The detail permission, community scope and editable-field calculation stay
    identical to the normal task detail endpoint.  A row that moved out of the
    current scope is returned as unavailable instead of failing the whole page.
    """
    normalized_keys = list(dict.fromkeys(
        str(row_key).strip() for row_key in row_keys if str(row_key).strip()
    ))
    if not normalized_keys:
        raise HTTPException(400, "请至少选择一条任务")

    items: dict[str, dict] = {}
    for row_key in normalized_keys:
        try:
            detail = await _mobile_task_detail_data(
                parser_type,
                row_key,
                user,
                conn,
                analysis_mode=analysis_mode,
                include_photo_requests=False,
            )
        except HTTPException as exc:
            if exc.status_code not in {404, 409}:
                raise
            items[row_key] = {
                "available": False,
                "reason": str(exc.detail),
            }
            continue
        if detail["task"]["conflict"] or len(detail["sources"]) != 1:
            items[row_key] = {
                "available": False,
                "reason": "该任务包含多个腾讯来源，请进入详情选择来源后修改",
            }
            continue
        items[row_key] = {
            "available": True,
            "detail": detail,
        }
    return {"items": items, "analysis_mode": analysis_mode}


@router.post("/analysis/{parser_type}/inline-editors")
async def get_mobile_task_analysis_inline_editors(
    parser_type: str,
    data: InlineEditorRequest,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    return await _mobile_task_inline_editors_data(
        parser_type,
        data.row_keys,
        user,
        conn,
        analysis_mode=True,
    )


@router.post("/{parser_type}/inline-editors")
async def get_mobile_task_inline_editors(
    parser_type: str,
    data: InlineEditorRequest,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    return await _mobile_task_inline_editors_data(
        parser_type,
        data.row_keys,
        user,
        conn,
    )


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

    return await queue_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes=data.changes,
        base_values=data.base_values,
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
    return await queue_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes=data.changes,
        base_values=data.base_values,
        expected_revision=data.expected_revision,
        request=request,
        user=user,
        conn=conn,
        explicit_text_edit=True,
    )


@router.post("/{parser_type}/source-rows/{source_id}/resolve-sync-conflict")
async def resolve_mobile_task_sync_conflict(
    parser_type: str,
    source_id: int,
    data: SyncConflictResolution,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    fields = list(dict.fromkeys(str(field).strip() for field in data.fields if str(field).strip()))
    if not fields:
        raise HTTPException(400, "请选择需要处理的冲突字段")

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT row_key FROM _online_source_rows WHERE id=%s AND parser_type=%s",
            (source_id, parser_type),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "来源行不存在")
        row_key = str(row[0])

    analysis_fields = set(TASK_WORKFLOWS[parser_type].analysis_fields)
    analysis_mode = any(field in analysis_fields for field in fields)
    if analysis_mode and any(field not in analysis_fields for field in fields):
        raise HTTPException(400, "核查字段和研判字段的冲突请分别处理")
    scoped_user = (
        _require_analysis_user(user)
        if analysis_mode else _require_task_edit_user(user)
    )
    detail = await _mobile_task_detail_data(
        parser_type,
        row_key,
        scoped_user,
        conn,
        analysis_mode=analysis_mode,
        include_photo_requests=False,
    )
    source = next((item for item in detail["sources"] if item["id"] == source_id), None)
    if not source:
        raise HTTPException(404, "来源行不存在或不属于当前社区")
    conflict_fields = {
        item["field"] for item in source.get("sync_fields", [])
        if item.get("status") == "conflict"
    }
    if any(field not in conflict_fields for field in fields):
        raise HTTPException(409, "所选字段已不处于同步冲突状态，请刷新后重试")
    if any(field not in source.get("editable_fields", []) for field in fields):
        raise HTTPException(403, "当前账号无权处理所选字段")

    try:
        result = await resolve_source_conflict(
            conn,
            source_id=source_id,
            choice=data.choice,
            fields=fields,
        )
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    await record_admin_audit(
        scoped_user,
        "online.writeback.resolve_conflict",
        target_type="online_source_row",
        target_name=f"{parser_type}:{source_id}",
        detail={"choice": data.choice, "columns": fields},
        **request_audit_fields(request),
    )
    if data.choice == "platform":
        launch_local_change_processing(source_id)
    return {
        "message": (
            "已采用平台值，正在重新同步腾讯表格"
            if data.choice == "platform"
            else "已采用腾讯值"
        ),
        **result,
    }


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
    row is still revalidated and written through the existing safe path.  Each
    request is capped to a small resumable chunk; balanced_offset/total keep the
    allocation deterministic across retries without bypassing per-row checks.
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
    balanced_total = data.balanced_total or len(row_keys)
    if data.mode == "balanced" and (
        data.balanced_offset + len(row_keys) > balanced_total
    ):
        raise HTTPException(400, "平均分配分块范围无效，请重新发起分配")

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
            row_keys,
            available_inspectors,
            total_count=balanced_total,
            start_index=data.balanced_offset,
        )
    else:
        assignment_plan = {row_key: inspector for row_key in eligible_keys}
        assignment_counts = {
            inspector: len(eligible_keys),
        }

    update_count = 0
    failures: list[dict[str, str]] = []
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
                await queue_source_fields(
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
                reason = {
                    400: "数据校验未通过",
                    403: "没有该任务的编辑权限",
                    409: "任务已变化，请刷新后重试",
                    502: "腾讯回写校验失败",
                }.get(exc.status_code, "保存失败")
                failures.append({"row_key": row_key, "reason": reason})
                break
        if task_ok:
            update_count += 1
            successful_assignment_counts[assigned_inspector] = (
                successful_assignment_counts.get(assigned_inspector, 0) + 1
            )

    result = _bulk_assignment_result(
        updated=update_count,
        skipped=skipped,
        failures=failures,
        inspector=inspector,
        mode=data.mode,
        assignment_counts=successful_assignment_counts,
    )
    await record_admin_audit(
        user,
        "mobile_tasks.bulk_assign",
        target_type="mobile_task",
        target_name=parser_type,
        detail={
            "task_count": len(row_keys),
            "updated_count": result["updated"],
            "skipped_count": result["skipped"],
            "failed_count": result["failed"],
            "inspector": inspector if data.mode == "single" else "",
            "allocation_mode": data.mode,
            "assignment_counts": successful_assignment_counts,
        },
        **request_audit_fields(request),
    )
    return result
