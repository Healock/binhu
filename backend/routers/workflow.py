"""通用工单底座 API（请假、照片调取等类型共用）。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import db_manager
from config import settings
from deps import require_permission, require_super_admin
from services.permissions import (
    WORKFLOW_CONFIG_MANAGE,
    WORKFLOW_TICKET_CREATE,
    WORKFLOW_TICKET_HANDLE,
    WORKFLOW_TICKET_MANAGE,
    WORKFLOW_TICKET_VIEW,
)
from services.workflow_support import (
    platform_schema,
    queue_user_ids,
    workflow_can_view_all,
    workflow_community_scope,
    workflow_notification,
)
from services.registry_security import hmac_digest, normalize_identity
from services.photo_sheet_sync import enqueue_outbox, launch_outbox_processing
from services.audit import record_admin_audit, request_audit_fields
from services.photo_import import canonical_photo_filename, is_generated_photo_filename


router = APIRouter(prefix="/api/workflow", tags=["工单"])


class TicketCreate(BaseModel):
    type_code: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    priority: str = Field(default="normal", max_length=20)
    form_data: dict = Field(default_factory=dict)
    links: list[dict] = Field(default_factory=list, max_length=20)


class WorkflowTypeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    form_schema: dict = Field(default_factory=lambda: {"fields": []})
    default_due_hours: int | None = Field(default=None, ge=1, le=24 * 365)


class CommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5000)
    expected_version: int = Field(gt=0)


class ClaimPayload(BaseModel):
    expected_version: int = Field(gt=0)


class Decision(BaseModel):
    expected_version: int = Field(gt=0)
    action: str = Field(pattern="^(approve|reject|return|complete|cancel)$")
    note: str = Field(default="", max_length=2000)
    result_status: str = Field(default="", pattern="^(|found|not_found)$")


def _photo_form_values(form_data: dict) -> dict[str, str]:
    subject_name = str(form_data.get("subject_name") or "").strip()
    identity_number = normalize_identity(str(form_data.get("identity_number") or ""))
    if not subject_name:
        raise HTTPException(422, "请填写对象姓名")
    if not re.fullmatch(r"(?:\d{15}|\d{17}[0-9X])", identity_number):
        raise HTTPException(422, "请填写有效身份证号")
    identity_hmac, hmac_version = hmac_digest(identity_number, kind="identity")
    return {
        "subject_name": subject_name[:100],
        "identity_number": identity_number[:50],
        "identity_hmac": identity_hmac or "",
        "identity_hmac_version": str(hmac_version),
        "source_parser_type": str(form_data.get("source_parser_type") or "")[:100],
        "source_row_key": str(form_data.get("source_row_key") or "")[:190],
        "community_name": str(form_data.get("community_name") or "")[:200],
        "source_label": str(form_data.get("source_label") or "")[:200],
    }


async def get_workflow_db():
    if not settings.WORKFLOW_FEATURE_ENABLED:
        raise HTTPException(503, "工单功能尚未完成生产迁移和启用")
    try:
        pool = db_manager.get_pool("workflow")
    except ValueError as exc:
        raise HTTPException(503, "工单数据库尚未完成初始化") from exc
    conn = await pool.acquire()
    try:
        yield conn
    finally:
        pool.release(conn)


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _json(value, default):
    if isinstance(value, type(default)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(default)) else default
        except (TypeError, ValueError):
            return default
    return default


def _validate_form_data(schema_value, form_data: dict) -> dict:
    schema = _json(schema_value, {})
    fields = schema.get("fields") if isinstance(schema, dict) else []
    if not isinstance(fields, list):
        raise HTTPException(409, "工单表单配置无效")
    normalized: dict[str, object] = {}
    for raw_field in fields[:100]:
        if not isinstance(raw_field, dict):
            continue
        name = str(raw_field.get("name") or "").strip()
        if not name or len(name) > 60 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise HTTPException(409, "工单表单字段名称无效")
        value = form_data.get(name)
        if raw_field.get("required") and (value is None or value == "" or value == []):
            raise HTTPException(422, f"请填写{raw_field.get('label') or name}")
        field_type = str(raw_field.get("type") or "text")
        if value in (None, ""):
            normalized[name] = None if value is None else ""
            continue
        if field_type == "select":
            options = [str(option) for option in raw_field.get("options") or []]
            if str(value) not in options:
                raise HTTPException(422, f"{raw_field.get('label') or name}选项无效")
        if field_type == "number":
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, f"{raw_field.get('label') or name}必须是数字") from exc
        if isinstance(value, str) and len(value) > 5000:
            raise HTTPException(422, f"{raw_field.get('label') or name}内容过长")
        normalized[name] = value
    return normalized


def _can_manage(user: dict) -> bool:
    return WORKFLOW_TICKET_MANAGE in set(user.get("permissions") or [])


async def _formal_community(cur, value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    schema = platform_schema().replace("`", "")
    await cur.execute(
        f"SELECT community.name FROM `{schema}`._communities community WHERE community.name=%s "
        f"UNION SELECT community.name FROM `{schema}`._community_aliases alias "
        f"JOIN `{schema}`._communities community ON community.id=alias.community_id "
        "WHERE alias.alias=%s LIMIT 1",
        (normalized, normalized),
    )
    row = await cur.fetchone()
    return str(row[0]).strip() if row else ""


async def _can_view_ticket(cur, ticket_id: int, user: dict) -> tuple:
    """返回工单访问摘要；所有详情、评论和附件共用同一范围校验。"""
    await cur.execute(
        "SELECT requester_user_id, current_assignee_user_id, current_queue, status, version_no "
        "FROM work_orders WHERE id=%s",
        (ticket_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "工单不存在")
    if workflow_can_view_all(user) or user["id"] in {row[0], row[1]}:
        return row
    await cur.execute(
        "SELECT 1 FROM work_order_events WHERE work_order_id=%s AND actor_user_id=%s LIMIT 1",
        (ticket_id, user["id"]),
    )
    if await cur.fetchone():
        return row
    position = str((user.get("member") or {}).get("position") or "")
    if (
        row[2]
        and str(row[2]) == position
        and WORKFLOW_TICKET_HANDLE in set(user.get("permissions") or [])
    ):
        return row
    community_scope = workflow_community_scope(user, "scope_order")
    if community_scope:
        await cur.execute(
            "SELECT 1 FROM work_orders scope_order WHERE scope_order.id=%s AND "
            + community_scope[0]
            + " LIMIT 1",
            (ticket_id, *community_scope[1]),
        )
        if await cur.fetchone():
            return row
    raise HTTPException(403, "无权查看该工单")


async def _published_steps(cur, version_id: int) -> list[dict]:
    await cur.execute(
        "SELECT id, step_order, name, step_type, default_due_hours, config_json "
        "FROM workflow_steps WHERE workflow_version_id=%s ORDER BY step_order",
        (version_id,),
    )
    return [
        {
            "id": int(row[0]), "order": int(row[1]), "name": str(row[2]),
            "type": str(row[3]), "due_hours": row[4], "config": _json(row[5], {}),
        }
        for row in await cur.fetchall()
    ]


def _step_queue(step: dict) -> str:
    return str(step.get("config", {}).get("queue") or step.get("name") or "").strip()


async def _apply_leave_attendance(cur, ticket_id: int, actor_user_id: int) -> int | None:
    await cur.execute(
        "SELECT member_id, leave_type, start_date, end_date, reason, attendance_record_id "
        "FROM leave_request_details WHERE work_order_id=%s FOR UPDATE",
        (ticket_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    if row[5]:
        return int(row[5])
    schema = platform_schema().replace("`", "")
    await cur.execute(
        f"INSERT INTO `{schema}`._personnel_attendance_history "
        "(member_id, absence_type, start_date, end_date, reason, source, created_by, is_active) "
        "VALUES (%s,%s,%s,%s,%s,'workflow',%s,1)",
        (row[0], row[1], row[2], row[3], str(row[4] or "")[:200], actor_user_id),
    )
    attendance_id = int(cur.lastrowid)
    await cur.execute(
        "UPDATE leave_request_details SET attendance_record_id=%s WHERE work_order_id=%s AND attendance_record_id IS NULL",
        (attendance_id, ticket_id),
    )
    return attendance_id


async def _deactivate_leave_attendance(cur, ticket_id: int) -> None:
    await cur.execute(
        "SELECT attendance_record_id FROM leave_request_details WHERE work_order_id=%s",
        (ticket_id,),
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return
    schema = platform_schema().replace("`", "")
    await cur.execute(
        f"UPDATE `{schema}`._personnel_attendance_history SET is_active=0 "
        "WHERE id=%s AND source='workflow'",
        (row[0],),
    )


def _ticket_payload(row):
    return {
        "id": int(row[0]),
        "ticket_no": row[1],
        "type_code": row[2],
        "workflow_version_id": int(row[3]),
        "title": row[4],
        "description": row[5],
        "requester_user_id": int(row[6]) if row[6] is not None else None,
        "current_assignee_user_id": row[7],
        "current_queue": row[8],
        "status": row[9],
        "priority": row[10],
        "due_at": _iso(row[11]),
        "version_no": int(row[12]),
        "submitted_at": _iso(row[13]),
        "completed_at": _iso(row[14]),
        "created_at": _iso(row[15]),
        "updated_at": _iso(row[16]),
        "overdue": bool(row[11] and row[9] not in {"completed", "cancelled", "rejected"}
                         and row[11] < datetime.utcnow()),
        "form_data": row[17] if isinstance(row[17], dict) else json.loads(row[17] or "{}"),
    }


@router.get("/types")
async def list_workflow_types(
    user: dict = Depends(require_permission(WORKFLOW_TICKET_VIEW)),
    conn=Depends(get_workflow_db),
):
    type_clause = "" if user.get("role") == "super_admin" else " WHERE enabled=1"
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, code, name, description, form_schema, default_due_hours, enabled "
            f"FROM workflow_types{type_clause} ORDER BY id"
        )
        rows = await cur.fetchall()
    return {"data": [
        {"id": int(row[0]), "code": row[1], "name": row[2], "description": row[3],
         "form_schema": row[4], "default_due_hours": row[5], "enabled": bool(row[6])}
        for row in rows
    ]}


@router.post("/types")
async def create_workflow_type(
    data: WorkflowTypeCreate,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_workflow_db),
):
    code = data.code.strip()
    name = data.name.strip()
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO workflow_types (code, name, description, form_schema, default_due_hours, enabled, created_by) "
                "VALUES (%s,%s,%s,%s,%s,0,%s)",
                (
                    code,
                    name,
                    data.description.strip(),
                    json.dumps(data.form_schema, ensure_ascii=False),
                    data.default_due_hours,
                    user["id"],
                ),
            )
            type_id = int(cur.lastrowid)
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        if "duplicate" in str(exc).lower():
            raise HTTPException(409, "工单类型代码已存在") from exc
        raise
    return {"id": type_id, "message": "工单类型已创建，等待发布流程"}


@router.post("/types/{type_id}/publish")
async def publish_workflow_type(
    type_id: int,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT code, form_schema FROM workflow_types WHERE id=%s FOR UPDATE", (type_id,))
            workflow_type = await cur.fetchone()
            if not workflow_type:
                raise HTTPException(404, "工单类型不存在")
            await cur.execute(
                "SELECT COALESCE(MAX(version_no), 0) FROM workflow_type_versions WHERE workflow_type_id=%s",
                (type_id,),
            )
            version_no = int((await cur.fetchone())[0] or 0) + 1
            await cur.execute(
                "INSERT INTO workflow_type_versions "
                "(workflow_type_id, version_no, form_schema, status, published_by, published_at) "
                "VALUES (%s,%s,%s,'published',%s,UTC_TIMESTAMP())",
                (type_id, version_no, workflow_type[1], user["id"]),
            )
            version_id = int(cur.lastrowid)
            await cur.execute("UPDATE workflow_types SET enabled=1, updated_by=%s WHERE id=%s", (user["id"], type_id))
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"version_id": version_id, "version_no": version_no, "message": "工单流程已发布"}


@router.get("/tickets")
async def list_tickets(
    status: str | None = Query(default=None, max_length=30),
    type_code: str | None = Query(default=None, max_length=60),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_permission(WORKFLOW_TICKET_VIEW)),
    conn=Depends(get_workflow_db),
):
    where: list[str] = []
    params: list[object] = []
    if not workflow_can_view_all(user):
        direct = "(requester_user_id=%s OR current_assignee_user_id=%s)"
        params.extend([user["id"], user["id"]])
        community_scope = workflow_community_scope(user)
        if community_scope:
            where.append(f"({direct} OR {community_scope[0]})")
            params.extend(community_scope[1])
        else:
            where.append(direct)
    if status:
        where.append("status=%s")
        params.append(status)
    if type_code:
        where.append("type_code=%s")
        params.append(type_code)
    clause = " WHERE " + " AND ".join(where) if where else ""
    offset = (page - 1) * page_size
    columns = "id, ticket_no, type_code, workflow_version_id, title, description, requester_user_id, current_assignee_user_id, current_queue, status, priority, due_at, version_no, submitted_at, completed_at, created_at, updated_at, form_data"
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT COUNT(*) FROM work_orders{clause}", tuple(params))
        total = int((await cur.fetchone())[0])
        await cur.execute(
            f"SELECT {columns} FROM work_orders{clause} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            tuple(params) + (page_size, offset),
        )
        rows = await cur.fetchall()
    return {"total": total, "page": page, "page_size": page_size, "data": [_ticket_payload(row) for row in rows]}


@router.post("/tickets")
async def create_ticket(
    data: TicketCreate,
    user: dict = Depends(require_permission(WORKFLOW_TICKET_CREATE)),
    conn=Depends(get_workflow_db),
):
    photo_outbox_ticket_id: int | None = None
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, default_due_hours FROM workflow_types WHERE code=%s AND enabled=1",
                (data.type_code.strip(),),
            )
            workflow_type = await cur.fetchone()
            if not workflow_type:
                raise HTTPException(400, "工单类型未启用")
            await cur.execute(
                "SELECT id, form_schema FROM workflow_type_versions WHERE workflow_type_id=%s AND status='published' "
                "ORDER BY version_no DESC LIMIT 1",
                (workflow_type[0],),
            )
            version = await cur.fetchone()
            if not version:
                raise HTTPException(400, "工单类型尚未发布流程")
            submitted_form_data = dict(data.form_data)
            is_task_photo_request = bool(
                data.type_code.strip() == "photo_request"
                and str(submitted_form_data.get("source_parser_type") or "").strip()
                and str(submitted_form_data.get("source_row_key") or "").strip()
            )
            if is_task_photo_request and not str(submitted_form_data.get("request_reason") or "").strip():
                # 已有已发布流程仍可能把申请理由标为必填。快捷入口不再让
                # 网格员重复填写，用内部占位通过旧流程校验，详情中仍保存为空。
                submitted_form_data["request_reason"] = "在线任务快捷申请"
            form_data = _validate_form_data(version[1], submitted_form_data)
            steps = await _published_steps(cur, int(version[0]))
            if not steps:
                raise HTTPException(409, "工单流程没有可执行节点")
            first_step = steps[0]
            queue = _step_queue(first_step)
            if not queue:
                raise HTTPException(409, "工单首节点尚未配置处理队列")
            due_hours = first_step["due_hours"] or workflow_type[1]
            due_at = datetime.utcnow() + timedelta(hours=int(due_hours)) if due_hours else None
            await cur.execute(
                "INSERT INTO work_orders (ticket_no, type_code, workflow_version_id, title, description, "
                "requester_user_id, current_queue, status, priority, due_at, submitted_at, form_data) "
                "VALUES ('PENDING',%s,%s,%s,%s,%s,%s,'queued',%s,%s,UTC_TIMESTAMP(),%s)",
                (data.type_code.strip(), version[0], data.title.strip(), data.description.strip(),
                 user["id"], queue, data.priority.strip(), due_at,
                 json.dumps(form_data, ensure_ascii=False)),
            )
            ticket_id = int(cur.lastrowid)
            ticket_no = f"{data.type_code.strip().upper()[:12]}-{datetime.utcnow():%Y%m%d}-{ticket_id:06d}"
            await cur.execute("UPDATE work_orders SET ticket_no=%s WHERE id=%s", (ticket_no, ticket_id))
            now = datetime.utcnow()
            for index, step in enumerate(steps):
                step_due = now + timedelta(hours=int(step["due_hours"])) if step["due_hours"] else None
                await cur.execute(
                    "INSERT INTO work_order_steps "
                    "(work_order_id, workflow_step_id, step_order, status, queue, due_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (ticket_id, step["id"], step["order"], "queued" if index == 0 else "pending",
                     _step_queue(step), step_due if index == 0 else None),
                )
            for link in data.links:
                object_type = str(link.get("object_type") or "").strip()
                object_id = str(link.get("object_id") or "").strip()
                if object_type and object_id:
                    await cur.execute(
                        "INSERT IGNORE INTO work_order_links (work_order_id, object_type, object_id, object_ref) "
                        "VALUES (%s,%s,%s,%s)",
                        (ticket_id, object_type, object_id, str(link.get("object_ref") or "")[:190]),
                    )
            await cur.execute(
                "INSERT INTO work_order_events (work_order_id, event_type, actor_user_id, from_status, "
                "to_status, detail_json) VALUES (%s,'submit',%s,'draft','queued',%s)",
                (ticket_id, user["id"], json.dumps({"type_code": data.type_code}, ensure_ascii=False)),
            )
            if data.type_code == "photo_request":
                photo_values = _photo_form_values(data.form_data)
                if is_task_photo_request:
                    formal_community = await _formal_community(cur, photo_values["community_name"])
                    if not formal_community:
                        raise HTTPException(422, "当前任务社区无法归一，暂时不能写入调照片名单")
                    photo_values["community_name"] = formal_community
                await cur.execute(
                    "INSERT INTO photo_request_details "
                    "(work_order_id, subject_type, subject_id, subject_name, identity_number, identity_hmac, "
                    "identity_hmac_version, source_parser_type, source_row_key, community_name, source_label, "
                    "requester_name_snapshot, requested_at, external_origin, external_sync_status, request_reason, "
                    "requested_from, requested_to) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (ticket_id, str(form_data.get("subject_type") or "")[:40],
                     str(form_data.get("subject_id") or "")[:190],
                     photo_values["subject_name"], photo_values["identity_number"],
                     photo_values["identity_hmac"], int(photo_values["identity_hmac_version"]),
                     photo_values["source_parser_type"], photo_values["source_row_key"],
                     photo_values["community_name"], photo_values["source_label"],
                     str((user.get("member") or {}).get("name") or user.get("display_name") or user.get("username") or "")[:100],
                     datetime.utcnow(),
                     "platform_task" if photo_values["source_parser_type"] and photo_values["source_row_key"] else "platform",
                     "pending" if photo_values["source_parser_type"] and photo_values["source_row_key"] else "not_linked",
                     "" if is_task_photo_request else str(form_data.get("request_reason") or "")[:1000],
                     form_data.get("requested_from") or None,
                     form_data.get("requested_to") or None),
                )
                if photo_values["source_parser_type"] and photo_values["source_row_key"]:
                    if await enqueue_outbox(cur, ticket_id, "append_request"):
                        photo_outbox_ticket_id = ticket_id
            elif data.type_code == "leave_request":
                member_id = (user.get("member") or {}).get("id")
                if not member_id:
                    raise HTTPException(422, "请假工单必须由已关联人员的账号发起")
                start_date = form_data.get("start_date")
                end_date = form_data.get("end_date")
                if not start_date or not end_date or str(end_date) < str(start_date):
                    raise HTTPException(422, "请假起止日期无效")
                await cur.execute(
                    "INSERT INTO leave_request_details "
                    "(work_order_id, member_id, leave_type, start_date, end_date, reason, affects_weekend_duty) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (ticket_id, member_id, str(form_data.get("leave_type") or "temporary_leave")[:50],
                     start_date, end_date, str(form_data.get("reason") or "")[:1000],
                     int(bool(form_data.get("affects_weekend_duty")))),
                )
            recipients = await queue_user_ids(cur, queue)
            await workflow_notification(
                cur, user_ids=recipients, ticket_id=ticket_id,
                event_key="submit", title="有新的待领取工单",
                content=f"工单 {ticket_no} 已进入“{queue}”队列。",
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    if photo_outbox_ticket_id is not None:
        launch_outbox_processing(photo_outbox_ticket_id)
    return {"id": ticket_id, "ticket_no": ticket_no, "message": "工单已提交"}


@router.get("/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: int,
    user: dict = Depends(require_permission(WORKFLOW_TICKET_VIEW)),
    conn=Depends(get_workflow_db),
):
    async with conn.cursor() as cur:
        await _can_view_ticket(cur, ticket_id, user)
        await cur.execute(
            "SELECT id, ticket_no, type_code, workflow_version_id, title, description, requester_user_id, current_assignee_user_id, current_queue, status, priority, due_at, version_no, submitted_at, completed_at, created_at, updated_at, form_data FROM work_orders WHERE id=%s",
            (ticket_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "工单不存在")
        await cur.execute("SELECT event_type, actor_user_id, from_status, to_status, detail_json, created_at FROM work_order_events WHERE work_order_id=%s ORDER BY id", (ticket_id,))
        events = await cur.fetchall()
        await cur.execute("SELECT user_id, content, created_at FROM work_order_comments WHERE work_order_id=%s ORDER BY id", (ticket_id,))
        comments = await cur.fetchall()
        await cur.execute(
            "SELECT order_step.id, order_step.step_order, definition.name, definition.step_type, "
            "order_step.status, order_step.assignee_user_id, order_step.queue, order_step.due_at, "
            "order_step.decision, order_step.decision_note, order_step.decided_by, order_step.decided_at, "
            "order_step.version_no FROM work_order_steps order_step "
            "JOIN workflow_steps definition ON definition.id=order_step.workflow_step_id "
            "WHERE order_step.work_order_id=%s ORDER BY order_step.step_order",
            (ticket_id,),
        )
        steps = await cur.fetchall()
        await cur.execute(
            "SELECT object_type, object_id, object_ref FROM work_order_links "
            "WHERE work_order_id=%s ORDER BY id",
            (ticket_id,),
        )
        links = await cur.fetchall()
        await cur.execute(
            "SELECT file_id, original_name, mime_type, size_bytes, sha256, retention_until, deleted_at, created_at "
            "FROM work_order_attachments WHERE work_order_id=%s ORDER BY id DESC",
            (ticket_id,),
        )
        attachments = await cur.fetchall()
        detail_row = None
        if row[2] == "leave_request":
            await cur.execute(
                "SELECT member_id, leave_type, start_date, end_date, reason, affects_weekend_duty, attendance_record_id "
                "FROM leave_request_details WHERE work_order_id=%s",
                (ticket_id,),
            )
            detail_row = await cur.fetchone()
        elif row[2] == "photo_request":
            await cur.execute(
                "SELECT subject_type, subject_id, subject_name, identity_number, source_parser_type, source_row_key, "
                "requested_from, requested_to, request_reason, result_status, result_note, community_name, "
                "source_label, requester_name_snapshot, requested_at, external_origin, external_sync_status, "
                "legacy_result_note, data_issue, batch.completed_at, mapping.physical_row, mapping.batch_id, "
                "mapping.sync_status FROM photo_request_details detail "
                "LEFT JOIN photo_sheet_rows mapping ON mapping.work_order_id=detail.work_order_id "
                "LEFT JOIN photo_sheet_batches batch ON batch.id=mapping.batch_id "
                "WHERE detail.work_order_id=%s",
                (ticket_id,),
            )
            detail_row = await cur.fetchone()
    payload = _ticket_payload(row)
    payload["events"] = [{"event_type": x[0], "actor_user_id": x[1], "from_status": x[2], "to_status": x[3], "detail": x[4], "created_at": _iso(x[5])} for x in events]
    payload["comments"] = [{"user_id": x[0], "content": x[1], "created_at": _iso(x[2])} for x in comments]
    payload["steps"] = [
        {
            "id": int(item[0]), "step_order": int(item[1]), "name": item[2],
            "step_type": item[3], "status": item[4], "assignee_user_id": item[5],
            "queue": item[6], "due_at": _iso(item[7]), "decision": item[8],
            "decision_note": item[9], "decided_by": item[10],
            "decided_at": _iso(item[11]), "version_no": int(item[12]),
        }
        for item in steps
    ]
    payload["links"] = [
        {"object_type": item[0], "object_id": item[1], "object_ref": item[2]}
        for item in links
    ]
    payload["attachments"] = [
        {
            "file_id": item[0],
            "original_name": (
                canonical_photo_filename(
                    str(detail_row[2] or ""), str(detail_row[3] or ""),
                    ".jpg" if item[2] == "image/jpeg" else ".png" if item[2] == "image/png" else ".webp" if item[2] == "image/webp" else ".heic",
                )
                if row[2] == "photo_request" and detail_row and is_generated_photo_filename(item[1])
                else item[1]
            ),
            "mime_type": item[2],
            "size_bytes": int(item[3]), "sha256": item[4],
            "retention_until": _iso(item[5]), "deleted_at": _iso(item[6]),
            "created_at": _iso(item[7]),
        }
        for item in attachments
    ]
    if row[2] == "leave_request" and detail_row:
        payload["type_detail"] = {
            "member_id": int(detail_row[0]), "leave_type": detail_row[1],
            "start_date": str(detail_row[2]), "end_date": str(detail_row[3]),
            "reason": detail_row[4], "affects_weekend_duty": bool(detail_row[5]),
            "attendance_record_id": detail_row[6],
        }
    elif row[2] == "photo_request" and detail_row:
        payload["type_detail"] = {
            "subject_type": detail_row[0], "subject_id": detail_row[1],
            "subject_name": detail_row[2], "identity_number": detail_row[3],
            "source_parser_type": detail_row[4], "source_row_key": detail_row[5],
            "requested_from": _iso(detail_row[6]), "requested_to": _iso(detail_row[7]),
            "request_reason": detail_row[8], "result_status": detail_row[9],
            "result_note": detail_row[10],
            "community_name": detail_row[11], "source_label": detail_row[12],
            "requester_name_snapshot": detail_row[13], "requested_at": _iso(detail_row[14]),
            "external_origin": detail_row[15], "external_sync_status": detail_row[16],
            "legacy_result_note": detail_row[17], "data_issue": detail_row[18],
            "batch_completed_at": _iso(detail_row[19]), "tencent_physical_row": detail_row[20],
            "photo_sheet_batch_id": detail_row[21], "row_sync_status": detail_row[22],
        }
    return payload


@router.post("/tickets/{ticket_id}/claim")
async def claim_ticket(
    ticket_id: int,
    data: ClaimPayload,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_TICKET_HANDLE)),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, current_assignee_user_id, version_no, current_queue, requester_user_id "
                "FROM work_orders WHERE id=%s FOR UPDATE",
                (ticket_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "工单不存在")
            if int(row[2]) != data.expected_version:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            if row[0] != "queued":
                raise HTTPException(409, "当前工单状态不能领取")
            if row[1] and int(row[1]) != int(user["id"]):
                raise HTTPException(409, "工单已被其他人领取")
            position = str((user.get("member") or {}).get("position") or "")
            if not _can_manage(user) and position != str(row[3] or ""):
                raise HTTPException(403, "当前工单不属于你的岗位队列")
            await cur.execute(
                "SELECT id FROM work_order_steps WHERE work_order_id=%s AND status='queued' "
                "ORDER BY step_order LIMIT 1 FOR UPDATE",
                (ticket_id,),
            )
            step = await cur.fetchone()
            if not step:
                raise HTTPException(409, "当前工单没有可领取节点")
            await cur.execute(
                "UPDATE work_orders SET current_assignee_user_id=%s, status='in_progress', "
                "version_no=version_no+1 WHERE id=%s AND current_assignee_user_id IS NULL "
                "AND status='queued' AND version_no=%s",
                (user["id"], ticket_id, data.expected_version),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "工单已被其他人领取")
            await cur.execute(
                "UPDATE work_order_steps SET assignee_user_id=%s, status='in_progress', "
                "version_no=version_no+1 WHERE id=%s AND status='queued'",
                (user["id"], step[0]),
            )
            await cur.execute(
                "INSERT INTO work_order_claims (work_order_id, step_id, user_id) VALUES (%s,%s,%s)",
                (ticket_id, step[0], user["id"]),
            )
            await cur.execute(
                "INSERT INTO work_order_events (work_order_id, event_type, actor_user_id, "
                "from_status, to_status, detail_json) VALUES (%s,'claim',%s,%s,'in_progress','{}')",
                (ticket_id, user["id"], row[0]),
            )
            await workflow_notification(
                cur, user_ids=[int(row[4])], ticket_id=ticket_id,
                event_key=f"claim_{int(row[2])}", title="工单已被领取",
                content=f"工单 #{ticket_id} 已进入处理。",
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "workflow.ticket.claim",
        target_type="work_order",
        target_name=str(ticket_id),
        detail={"from_status": "queued", "to_status": "in_progress"},
        **request_audit_fields(request),
    )
    return {"message": "工单已领取"}


@router.post("/tickets/{ticket_id}/decision")
async def decide_ticket(
    ticket_id: int,
    data: Decision,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_TICKET_HANDLE)),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, current_assignee_user_id, version_no, requester_user_id, type_code, current_queue "
                "FROM work_orders WHERE id=%s FOR UPDATE",
                (ticket_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "工单不存在")
            if int(row[2]) != data.expected_version:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            if row[1] and int(row[1]) != int(user["id"]) and not _can_manage(user):
                raise HTTPException(403, "当前工单未分配给你")
            if row[0] not in {"in_progress", "queued"}:
                raise HTTPException(409, "当前工单状态不能执行该操作")
            if row[4] == "photo_request" and data.action in {"approve", "complete"}:
                if not data.result_status:
                    raise HTTPException(422, "照片调取完成前请选择找到或未找到")
                await cur.execute(
                    "UPDATE photo_request_details SET result_status=%s, result_note=%s "
                    "WHERE work_order_id=%s",
                    (data.result_status, data.note.strip(), ticket_id),
                )
            await cur.execute(
                "SELECT order_step.id, order_step.workflow_step_id, order_step.step_order, "
                "order_step.status, definition.config_json FROM work_order_steps order_step "
                "JOIN workflow_steps definition ON definition.id=order_step.workflow_step_id "
                "WHERE order_step.work_order_id=%s AND order_step.status IN ('queued','in_progress') "
                "ORDER BY order_step.step_order LIMIT 1 FOR UPDATE",
                (ticket_id,),
            )
            step = await cur.fetchone()
            if not step:
                raise HTTPException(409, "当前工单没有可处理节点")
            await cur.execute(
                "SELECT 1 FROM work_order_steps WHERE work_order_id=%s AND status='pending' LIMIT 1",
                (ticket_id,),
            )
            has_pending_next_step = bool(await cur.fetchone())
            if data.action == "approve" and not has_pending_next_step:
                raise HTTPException(409, "当前已是最后一个流程节点，请使用“完成”")
            if data.action == "complete" and has_pending_next_step:
                raise HTTPException(409, "当前还有后续流程节点，请使用“通过”")
            if not row[1]:
                config = _json(step[4], {})
                if bool(config.get("claim_required", True)) and not _can_manage(user):
                    raise HTTPException(409, "请先领取该工单再处理")
                position = str((user.get("member") or {}).get("position") or "")
                if not _can_manage(user) and position != str(row[5] or ""):
                    raise HTTPException(403, "当前工单不属于你的岗位队列")

            next_status = ""
            next_queue = ""
            next_due = None
            terminal = data.action in {"reject", "cancel"}
            if data.action == "return":
                next_status = "pending_requester"
                await cur.execute(
                    "UPDATE work_order_steps SET status='pending_requester', decision='return', decision_note=%s, "
                    "decided_by=%s, decided_at=UTC_TIMESTAMP(), version_no=version_no+1 WHERE id=%s",
                    (data.note.strip(), user["id"], step[0]),
                )
                await cur.execute(
                    "UPDATE work_order_claims SET released_at=UTC_TIMESTAMP(), release_reason=%s "
                    "WHERE step_id=%s AND released_at IS NULL",
                    (data.note.strip(), step[0]),
                )
            elif terminal:
                next_status = "rejected" if data.action == "reject" else "cancelled"
                await cur.execute(
                    "UPDATE work_order_steps SET status=%s, decision=%s, decision_note=%s, decided_by=%s, "
                    "decided_at=UTC_TIMESTAMP(), version_no=version_no+1 WHERE id=%s",
                    (next_status, data.action, data.note.strip(), user["id"], step[0]),
                )
            else:
                step_done = "approved" if data.action == "approve" else "completed"
                await cur.execute(
                    "UPDATE work_order_steps SET status=%s, decision=%s, decision_note=%s, decided_by=%s, "
                    "decided_at=UTC_TIMESTAMP(), version_no=version_no+1 WHERE id=%s",
                    (step_done, data.action, data.note.strip(), user["id"], step[0]),
                )
                await cur.execute(
                    "SELECT order_step.id, definition.name, definition.default_due_hours, definition.config_json "
                    "FROM work_order_steps order_step JOIN workflow_steps definition ON definition.id=order_step.workflow_step_id "
                    "WHERE order_step.work_order_id=%s AND order_step.status='pending' "
                    "ORDER BY order_step.step_order LIMIT 1 FOR UPDATE",
                    (ticket_id,),
                )
                next_step = await cur.fetchone()
                if next_step:
                    config = _json(next_step[3], {})
                    next_queue = str(config.get("queue") or next_step[1] or "").strip()
                    if not next_queue:
                        raise HTTPException(409, "下一节点尚未配置处理队列")
                    next_due = datetime.utcnow() + timedelta(hours=int(next_step[2])) if next_step[2] else None
                    next_status = "queued"
                    await cur.execute(
                        "UPDATE work_order_steps SET status='queued', queue=%s, due_at=%s, version_no=version_no+1 "
                        "WHERE id=%s",
                        (next_queue, next_due, next_step[0]),
                    )
                else:
                    next_status = "approved" if data.action == "approve" else "completed"
                    terminal = True

            completed_at = datetime.utcnow() if terminal or next_status in {"approved", "completed", "rejected", "cancelled"} else None
            await cur.execute(
                "UPDATE work_orders SET status=%s, current_assignee_user_id=NULL, current_queue=%s, due_at=%s, "
                "completed_at=COALESCE(%s, completed_at), cancelled_at=CASE WHEN %s='cancelled' THEN UTC_TIMESTAMP() ELSE cancelled_at END, "
                "version_no=version_no+1 WHERE id=%s AND version_no=%s",
                (next_status, next_queue or row[5], next_due, completed_at, next_status, ticket_id, data.expected_version),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "工单版本已变化，请刷新后重试")
            if row[4] == "leave_request" and next_status in {"approved", "completed"}:
                await _apply_leave_attendance(cur, ticket_id, user["id"])
            elif row[4] == "leave_request" and next_status == "cancelled":
                await _deactivate_leave_attendance(cur, ticket_id)
            elif row[4] == "photo_request" and next_status in {"approved", "completed"} and data.result_status == "found":
                await cur.execute(
                    "SELECT external_origin FROM photo_request_details WHERE work_order_id=%s", (ticket_id,),
                )
                origin = await cur.fetchone()
                if origin and origin[0] in {"platform_task", "tencent"}:
                    await enqueue_outbox(cur, ticket_id, "mark_completed")
            if next_status in {"approved", "completed", "rejected", "cancelled"}:
                await cur.execute(
                    "UPDATE work_order_attachments SET retention_until=DATE_ADD(UTC_TIMESTAMP(), INTERVAL 90 DAY) "
                    "WHERE work_order_id=%s AND deleted_at IS NULL",
                    (ticket_id,),
                )
            await cur.execute(
                "INSERT INTO work_order_events (work_order_id, step_id, event_type, actor_user_id, from_status, "
                "to_status, detail_json) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (ticket_id, step[0], data.action, user["id"], row[0], next_status,
                 json.dumps({"note_length": len(data.note)}, ensure_ascii=False)),
            )
            recipients = [int(row[3])] if row[3] is not None else []
            if next_status == "queued":
                recipients.extend(await queue_user_ids(cur, next_queue))
            await workflow_notification(
                cur, user_ids=recipients, ticket_id=ticket_id,
                event_key=f"{data.action}_{data.expected_version}", title="工单状态已更新",
                content=f"工单 #{ticket_id} 当前状态：{next_status}。",
                severity="warning" if next_status in {"rejected", "pending_requester"} else "info",
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "workflow.ticket.decision",
        target_type="work_order",
        target_name=str(ticket_id),
        detail={
            "action": data.action,
            "from_status": row[0],
            "to_status": next_status,
            "result_status": data.result_status or None,
            "note_length": len(data.note),
        },
        **request_audit_fields(request),
    )
    return {"message": "工单状态已更新", "status": next_status}


@router.post("/tickets/{ticket_id}/comments")
async def add_comment(
    ticket_id: int,
    data: CommentCreate,
    user: dict = Depends(require_permission(WORKFLOW_TICKET_VIEW)),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            ticket = await _can_view_ticket(cur, ticket_id, user)
            if (
                user["id"] not in {ticket[0], ticket[1]}
                and WORKFLOW_TICKET_MANAGE not in set(user.get("permissions") or [])
            ):
                raise HTTPException(403, "无权评论该工单")
            await cur.execute(
                "SELECT version_no FROM work_orders WHERE id=%s FOR UPDATE",
                (ticket_id,),
            )
            version_row = await cur.fetchone()
            if not version_row or int(version_row[0]) != data.expected_version:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            await cur.execute(
                "INSERT INTO work_order_comments (work_order_id, user_id, content) VALUES (%s,%s,%s)",
                (ticket_id, user["id"], data.content.strip()),
            )
            await cur.execute(
                "UPDATE work_orders SET version_no=version_no+1 "
                "WHERE id=%s AND version_no=%s",
                (ticket_id, data.expected_version),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            await cur.execute(
                "INSERT INTO work_order_events (work_order_id, event_type, actor_user_id, detail_json) "
                "VALUES (%s,'comment',%s,%s)",
                (ticket_id, user["id"], json.dumps({"length": len(data.content)}, ensure_ascii=False)),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"message": "评论已添加"}
