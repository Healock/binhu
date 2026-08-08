"""工单流程配置、队列操作、附件和补充操作。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import settings
from deps import require_permission, require_super_admin
from routers.workflow import _can_view_ticket as workflow_ticket_access
from routers.workflow import _deactivate_leave_attendance, get_workflow_db
from services.audit import record_admin_audit, request_audit_fields
from services.permissions import (
    WORKFLOW_ATTACHMENT_VIEW,
    WORKFLOW_CONFIG_MANAGE,
    WORKFLOW_TICKET_HANDLE,
    WORKFLOW_TICKET_MANAGE,
    WORKFLOW_TICKET_VIEW,
)
from services.workflow_support import (
    MAX_ATTACHMENTS_PER_TICKET,
    SAFE_FILE_ID,
    platform_schema,
    queue_user_ids,
    remove_attachment,
    resolve_attachment,
    save_attachment,
    workflow_can_view_all,
    workflow_community_scope,
    workflow_notification,
)


router = APIRouter(prefix="/api/workflow", tags=["工单"])


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


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _can_manage(user: dict) -> bool:
    return WORKFLOW_TICKET_MANAGE in set(user.get("permissions") or [])


async def _can_view_ticket(cur, ticket_id: int, user: dict) -> tuple:
    return await workflow_ticket_access(cur, ticket_id, user)


class WorkflowStepPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    step_type: str = Field(default="approval", pattern="^(approval|handling)$")
    queue: str = Field(min_length=1, max_length=100)
    claim_required: bool = True
    due_hours: int | None = Field(default=None, ge=1, le=24 * 365)
    reminder_before_minutes: int | None = Field(default=None, ge=5, le=60 * 24 * 30)
    allow_transfer: bool = True


class WorkflowDraftPayload(BaseModel):
    form_schema: dict = Field(default_factory=lambda: {"fields": []})
    approval_mode: str = Field(default="sequential", pattern="^(sequential|any|all)$")
    steps: list[WorkflowStepPayload] = Field(min_length=1, max_length=30)


class TransferPayload(BaseModel):
    expected_version: int = Field(gt=0)
    target_user_id: int | None = Field(default=None, gt=0)
    target_queue: str = Field(default="", max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class SupplementPayload(BaseModel):
    expected_version: int = Field(gt=0)
    note: str = Field(default="", max_length=2000)
    form_data: dict = Field(default_factory=dict)


class WithdrawPayload(BaseModel):
    expected_version: int = Field(gt=0)
    reason: str = Field(default="", max_length=500)


class TicketSearch(BaseModel):
    view: str = Field(default="mine", pattern="^(mine|created|claimable|handling|supplement|processed|all)$")
    status: list[str] = Field(default_factory=list, max_length=20)
    type_code: str = Field(default="", max_length=60)
    keyword: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


@router.get("/types/{type_id}/versions")
async def list_workflow_versions(
    type_id: int,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, version_no, form_schema, approval_mode, status, published_at, created_at "
            "FROM workflow_type_versions WHERE workflow_type_id=%s ORDER BY version_no DESC",
            (type_id,),
        )
        rows = await cur.fetchall()
    return {"data": [
        {"id": int(row[0]), "version_no": int(row[1]), "form_schema": _json(row[2], {}),
         "approval_mode": row[3], "status": row[4], "published_at": _iso(row[5]),
         "created_at": _iso(row[6])} for row in rows
    ]}


@router.get("/versions/{version_id}")
async def get_workflow_version(
    version_id: int,
    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE)),
    conn=Depends(get_workflow_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, workflow_type_id, version_no, form_schema, approval_mode, status, published_at "
            "FROM workflow_type_versions WHERE id=%s",
            (version_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "流程版本不存在")
        await cur.execute(
            "SELECT id, step_order, name, step_type, default_due_hours, config_json "
            "FROM workflow_steps WHERE workflow_version_id=%s ORDER BY step_order",
            (version_id,),
        )
        steps = await cur.fetchall()
    return {
        "id": int(row[0]), "workflow_type_id": int(row[1]), "version_no": int(row[2]),
        "form_schema": _json(row[3], {}), "approval_mode": row[4], "status": row[5],
        "published_at": _iso(row[6]),
        "steps": [
            {"id": int(item[0]), "step_order": int(item[1]), "name": item[2],
             "step_type": item[3], "due_hours": item[4], **_json(item[5], {})} for item in steps
        ],
    }


@router.post("/types/{type_id}/versions")
async def create_workflow_draft(
    type_id: int,
    data: WorkflowDraftPayload,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM workflow_types WHERE id=%s FOR UPDATE", (type_id,))
            if not await cur.fetchone():
                raise HTTPException(404, "工单类型不存在")
            await cur.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM workflow_type_versions WHERE workflow_type_id=%s",
                (type_id,),
            )
            version_no = int((await cur.fetchone())[0])
            await cur.execute(
                "INSERT INTO workflow_type_versions "
                "(workflow_type_id, version_no, form_schema, approval_mode, status, created_by) "
                "VALUES (%s,%s,%s,%s,'draft',%s)",
                (type_id, version_no, json.dumps(data.form_schema, ensure_ascii=False), data.approval_mode, user["id"]),
            )
            version_id = int(cur.lastrowid)
            for order, step in enumerate(data.steps, start=1):
                config = {
                    "queue": step.queue.strip(), "claim_required": step.claim_required,
                    "reminder_before_minutes": step.reminder_before_minutes,
                    "allow_transfer": step.allow_transfer,
                }
                await cur.execute(
                    "INSERT INTO workflow_steps "
                    "(workflow_version_id, step_order, step_group, name, step_type, approval_mode, "
                    "default_due_hours, config_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (version_id, order, order, step.name.strip(), step.step_type, data.approval_mode,
                     step.due_hours, json.dumps(config, ensure_ascii=False)),
                )
                step_id = int(cur.lastrowid)
                await cur.execute(
                    "INSERT INTO workflow_step_assignee_rules "
                    "(step_id, assignee_type, assignee_value, priority) VALUES (%s,'position_queue',%s,100)",
                    (step_id, step.queue.strip()),
                )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "workflow.version.create", target_type="workflow_version",
        target_name=str(version_id), detail={"type_id": type_id, "step_count": len(data.steps)},
        **request_audit_fields(request),
    )
    return {"id": version_id, "version_no": version_no, "message": "流程草稿已保存"}


@router.put("/versions/{version_id}")
async def update_workflow_draft(
    version_id: int,
    data: WorkflowDraftPayload,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT status FROM workflow_type_versions WHERE id=%s FOR UPDATE", (version_id,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "流程版本不存在")
            if row[0] != "draft":
                raise HTTPException(409, "已发布流程不可修改")
            await cur.execute(
                "UPDATE workflow_type_versions SET form_schema=%s, approval_mode=%s WHERE id=%s",
                (json.dumps(data.form_schema, ensure_ascii=False), data.approval_mode, version_id),
            )
            await cur.execute(
                "DELETE rule FROM workflow_step_assignee_rules rule JOIN workflow_steps step ON step.id=rule.step_id "
                "WHERE step.workflow_version_id=%s",
                (version_id,),
            )
            await cur.execute("DELETE FROM workflow_steps WHERE workflow_version_id=%s", (version_id,))
            for order, step in enumerate(data.steps, start=1):
                config = {
                    "queue": step.queue.strip(), "claim_required": step.claim_required,
                    "reminder_before_minutes": step.reminder_before_minutes,
                    "allow_transfer": step.allow_transfer,
                }
                await cur.execute(
                    "INSERT INTO workflow_steps "
                    "(workflow_version_id, step_order, step_group, name, step_type, approval_mode, "
                    "default_due_hours, config_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (version_id, order, order, step.name.strip(), step.step_type, data.approval_mode,
                     step.due_hours, json.dumps(config, ensure_ascii=False)),
                )
                step_id = int(cur.lastrowid)
                await cur.execute(
                    "INSERT INTO workflow_step_assignee_rules (step_id, assignee_type, assignee_value) "
                    "VALUES (%s,'position_queue',%s)",
                    (step_id, step.queue.strip()),
                )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "workflow.version.update", target_type="workflow_version",
        target_name=str(version_id), detail={"step_count": len(data.steps)}, **request_audit_fields(request),
    )
    return {"message": "流程草稿已更新"}


@router.post("/versions/{version_id}/publish")
async def publish_workflow_version(
    version_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT workflow_type_id, status FROM workflow_type_versions WHERE id=%s FOR UPDATE",
                (version_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "流程版本不存在")
            if row[1] != "draft":
                raise HTTPException(409, "该流程版本不能重复发布")
            await cur.execute("SELECT COUNT(*) FROM workflow_steps WHERE workflow_version_id=%s", (version_id,))
            if int((await cur.fetchone())[0]) < 1:
                raise HTTPException(422, "流程至少需要一个节点")
            await cur.execute(
                "UPDATE workflow_type_versions SET status='published', published_by=%s, published_at=UTC_TIMESTAMP() "
                "WHERE id=%s",
                (user["id"], version_id),
            )
            await cur.execute(
                "UPDATE workflow_types SET enabled=1, updated_by=%s WHERE id=%s",
                (user["id"], row[0]),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "workflow.version.publish", target_type="workflow_version",
        target_name=str(version_id), detail={}, **request_audit_fields(request),
    )
    return {"message": "流程版本已发布，已有工单继续使用原版本"}


@router.post("/tickets/search")
async def search_tickets(
    data: TicketSearch,
    user: dict = Depends(require_permission(WORKFLOW_TICKET_VIEW)),
    conn=Depends(get_workflow_db),
):
    where: list[str] = []
    params: list[object] = []
    position = str((user.get("member") or {}).get("position") or "")
    if data.view == "created":
        where.append("requester_user_id=%s")
        params.append(user["id"])
    elif data.view == "claimable":
        if not position and not _can_manage(user):
            where.append("1=0")
        elif not _can_manage(user):
            where.append("current_queue=%s AND current_assignee_user_id IS NULL AND status='queued'")
            params.append(position)
        else:
            where.append("current_assignee_user_id IS NULL AND status='queued'")
    elif data.view == "handling":
        where.append("current_assignee_user_id=%s AND status='in_progress'")
        params.append(user["id"])
    elif data.view == "supplement":
        where.append("requester_user_id=%s AND status='pending_requester'")
        params.append(user["id"])
    elif data.view == "processed":
        where.append("EXISTS (SELECT 1 FROM work_order_events event WHERE event.work_order_id=work_orders.id AND event.actor_user_id=%s AND event.event_type IN ('claim','approve','reject','return','complete','transfer'))")
        params.append(user["id"])
    elif data.view == "all":
        if not workflow_can_view_all(user):
            raise HTTPException(403, "无权查看全部工单")
    else:
        if workflow_can_view_all(user):
            pass
        else:
            direct = "(requester_user_id=%s OR current_assignee_user_id=%s OR current_queue=%s)"
            params.extend([user["id"], user["id"], position])
            community_scope = workflow_community_scope(user)
            if community_scope:
                where.append(f"({direct} OR {community_scope[0]})")
                params.extend(community_scope[1])
            else:
                where.append(direct)
    if data.status:
        placeholders = ",".join(["%s"] * len(data.status))
        where.append(f"status IN ({placeholders})")
        params.extend(data.status)
    if data.type_code.strip():
        where.append("type_code=%s")
        params.append(data.type_code.strip())
    if data.keyword.strip():
        where.append("(ticket_no LIKE %s OR title LIKE %s)")
        params.extend([f"%{data.keyword.strip()}%", f"%{data.keyword.strip()}%"])
    clause = " WHERE " + " AND ".join(where) if where else ""
    columns = (
        "id, ticket_no, type_code, title, requester_user_id, current_assignee_user_id, current_queue, "
        "status, priority, due_at, version_no, submitted_at, completed_at, created_at, updated_at"
    )
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT COUNT(*) FROM work_orders{clause}", tuple(params))
        total = int((await cur.fetchone())[0])
        await cur.execute(
            f"SELECT {columns} FROM work_orders{clause} ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s",
            tuple(params) + (data.page_size, (data.page - 1) * data.page_size),
        )
        rows = await cur.fetchall()
    now = datetime.utcnow()
    return {"total": total, "page": data.page, "page_size": data.page_size, "data": [
        {"id": int(row[0]), "ticket_no": row[1], "type_code": row[2], "title": row[3],
         "requester_user_id": int(row[4]), "current_assignee_user_id": row[5], "current_queue": row[6],
         "status": row[7], "priority": row[8], "due_at": _iso(row[9]), "version_no": int(row[10]),
         "submitted_at": _iso(row[11]), "completed_at": _iso(row[12]), "created_at": _iso(row[13]),
         "updated_at": _iso(row[14]), "overdue": bool(row[9] and row[9] < now and row[7] not in {"approved", "completed", "rejected", "cancelled", "withdrawn"})}
        for row in rows
    ]}


@router.post("/tickets/{ticket_id}/transfer")
async def transfer_ticket(
    ticket_id: int,
    data: TransferPayload,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_TICKET_HANDLE)),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, current_assignee_user_id, current_queue, version_no FROM work_orders WHERE id=%s FOR UPDATE",
                (ticket_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "工单不存在")
            if int(row[3]) != data.expected_version:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            if row[0] not in {"queued", "in_progress"}:
                raise HTTPException(409, "当前工单状态不能转派")
            if row[1] != user["id"] and not _can_manage(user):
                raise HTTPException(403, "只能转派本人正在处理的工单")
            await cur.execute(
                "SELECT step.id, definition.config_json FROM work_order_steps step "
                "JOIN workflow_steps definition ON definition.id=step.workflow_step_id "
                "WHERE step.work_order_id=%s AND step.status IN ('queued','in_progress') "
                "ORDER BY step.step_order LIMIT 1 FOR UPDATE",
                (ticket_id,),
            )
            current_step = await cur.fetchone()
            if not current_step:
                raise HTTPException(409, "当前工单没有可转派节点")
            if not _can_manage(user) and not bool(_json(current_step[1], {}).get("allow_transfer", True)):
                raise HTTPException(403, "当前流程节点不允许转派")
            target_queue = data.target_queue.strip()
            target_user_id = data.target_user_id
            if target_user_id:
                schema = platform_schema().replace("`", "")
                await cur.execute(
                    f"SELECT user.id, member.position FROM `{schema}`._users user "
                    f"JOIN `{schema}`._grid_members member ON member.id=user.member_id "
                    "WHERE user.id=%s AND member.status='在岗'",
                    (target_user_id,),
                )
                target = await cur.fetchone()
                if not target:
                    raise HTTPException(404, "目标处理人不存在或当前不在岗")
                target_queue = str(target[1] or target_queue)
            if not target_queue:
                raise HTTPException(422, "请选择目标处理人或处理队列")
            next_status = "in_progress" if target_user_id else "queued"
            await cur.execute(
                "UPDATE work_orders SET current_assignee_user_id=%s, current_queue=%s, status=%s, "
                "version_no=version_no+1 WHERE id=%s AND version_no=%s",
                (target_user_id, target_queue, next_status, ticket_id, data.expected_version),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            await cur.execute(
                "UPDATE work_order_steps SET assignee_user_id=%s, queue=%s, status=%s, version_no=version_no+1 "
                "WHERE id=%s",
                (target_user_id, target_queue, next_status, current_step[0]),
            )
            await cur.execute(
                "UPDATE work_order_claims SET released_at=UTC_TIMESTAMP(), release_reason=%s "
                "WHERE step_id=%s AND released_at IS NULL",
                (data.reason.strip(), current_step[0]),
            )
            await cur.execute(
                "INSERT INTO work_order_events (work_order_id, step_id, event_type, actor_user_id, from_status, to_status, detail_json) "
                "VALUES (%s,%s,'transfer',%s,%s,%s,%s)",
                (ticket_id, current_step[0], user["id"], row[0], next_status,
                 json.dumps({"target_user_id": target_user_id, "target_queue": target_queue, "reason_length": len(data.reason)})),
            )
            recipients = [target_user_id] if target_user_id else await queue_user_ids(cur, target_queue)
            await workflow_notification(
                cur, user_ids=recipients, ticket_id=ticket_id,
                event_key=f"transfer_{data.expected_version}", title="收到转派工单",
                content=f"工单 #{ticket_id} 已转入你的处理范围。",
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "workflow.ticket.transfer", target_type="work_order", target_name=str(ticket_id),
        detail={"target_user_id": data.target_user_id, "target_queue": data.target_queue.strip()},
        **request_audit_fields(request),
    )
    return {"message": "工单已转派"}


@router.post("/tickets/{ticket_id}/supplement")
async def supplement_ticket(
    ticket_id: int,
    data: SupplementPayload,
    user: dict = Depends(require_permission(WORKFLOW_TICKET_VIEW)),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT requester_user_id, current_queue, status, version_no, form_data FROM work_orders WHERE id=%s FOR UPDATE",
                (ticket_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "工单不存在")
            if row[0] != user["id"]:
                raise HTTPException(403, "只有申请人可以补充材料")
            if row[2] != "pending_requester":
                raise HTTPException(409, "当前工单不处于待补充状态")
            if int(row[3]) != data.expected_version:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            await cur.execute(
                "SELECT step.id, definition.default_due_hours FROM work_order_steps step "
                "JOIN workflow_steps definition ON definition.id=step.workflow_step_id "
                "WHERE step.work_order_id=%s AND step.status='pending_requester' "
                "ORDER BY step.step_order LIMIT 1 FOR UPDATE",
                (ticket_id,),
            )
            step = await cur.fetchone()
            if not step:
                raise HTTPException(409, "待补充流程节点不存在")
            due_at = (
                datetime.utcnow() + timedelta(hours=int(step[1]))
                if step[1]
                else None
            )
            form_data = _json(row[4], {})
            form_data.update(data.form_data)
            await cur.execute(
                "UPDATE work_orders SET status='queued', form_data=%s, current_assignee_user_id=NULL, "
                "due_at=%s, version_no=version_no+1 WHERE id=%s AND version_no=%s",
                (json.dumps(form_data, ensure_ascii=False), due_at, ticket_id, data.expected_version),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            await cur.execute(
                "UPDATE work_order_steps SET status='queued', assignee_user_id=NULL, due_at=%s, "
                "version_no=version_no+1 WHERE id=%s",
                (due_at, step[0]),
            )
            await cur.execute(
                "INSERT INTO work_order_events (work_order_id, step_id, event_type, actor_user_id, from_status, to_status, detail_json) "
                "VALUES (%s,%s,'supplement',%s,'pending_requester','queued',%s)",
                (ticket_id, step[0], user["id"], json.dumps({"note_length": len(data.note), "field_count": len(data.form_data)})),
            )
            recipients = await queue_user_ids(cur, str(row[1] or ""))
            await workflow_notification(
                cur, user_ids=recipients, ticket_id=ticket_id,
                event_key=f"supplement_{data.expected_version}", title="申请人已补充工单材料",
                content=f"工单 #{ticket_id} 已补充并重新进入待领取队列。",
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"message": "补充材料已提交"}


@router.post("/tickets/{ticket_id}/withdraw")
async def withdraw_ticket(
    ticket_id: int,
    data: WithdrawPayload,
    request: Request,
    user: dict = Depends(require_permission(WORKFLOW_TICKET_VIEW)),
    conn=Depends(get_workflow_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT requester_user_id, current_assignee_user_id, status, version_no, type_code "
                "FROM work_orders WHERE id=%s FOR UPDATE",
                (ticket_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "工单不存在")
            if row[0] != user["id"]:
                raise HTTPException(403, "只有申请人可以撤回工单")
            terminal = {"approved", "completed", "rejected", "cancelled", "withdrawn"}
            if row[2] in terminal and not (
                row[4] == "leave_request" and row[2] in {"approved", "completed"}
            ):
                raise HTTPException(409, "当前工单已经结束，不能撤回")
            if int(row[3]) != data.expected_version:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            await cur.execute(
                "UPDATE work_orders SET status='withdrawn', cancelled_at=UTC_TIMESTAMP(), "
                "cancel_reason=%s, completed_at=COALESCE(completed_at, UTC_TIMESTAMP()), "
                "version_no=version_no+1 WHERE id=%s AND version_no=%s",
                (data.reason.strip(), ticket_id, data.expected_version),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            await cur.execute(
                "UPDATE work_order_steps SET status='cancelled', version_no=version_no+1 "
                "WHERE work_order_id=%s AND status NOT IN ('rejected','cancelled')",
                (ticket_id,),
            )
            if row[4] == "leave_request":
                await _deactivate_leave_attendance(cur, ticket_id)
            await cur.execute(
                "UPDATE work_order_attachments SET retention_until=DATE_ADD(UTC_TIMESTAMP(), INTERVAL 90 DAY) "
                "WHERE work_order_id=%s AND deleted_at IS NULL",
                (ticket_id,),
            )
            if row[1]:
                await workflow_notification(
                    cur, user_ids=[int(row[1])], ticket_id=ticket_id,
                    event_key=f"withdraw_{data.expected_version}", title="工单已撤回",
                    content=f"工单 #{ticket_id} 已由申请人撤回。",
                )
            await cur.execute(
                "INSERT INTO work_order_events (work_order_id, event_type, actor_user_id, "
                "from_status, to_status, detail_json) "
                "VALUES (%s,'withdraw',%s,%s,'withdrawn',%s)",
                (ticket_id, user["id"], row[2], json.dumps({"reason_length": len(data.reason)})),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "workflow.ticket.withdraw", target_type="work_order", target_name=str(ticket_id),
        detail={}, **request_audit_fields(request),
    )
    return {"message": "工单已撤回"}


@router.get("/tickets/{ticket_id}/attachments")
async def list_attachments(
    ticket_id: int,
    user: dict = Depends(require_permission(WORKFLOW_ATTACHMENT_VIEW)),
    conn=Depends(get_workflow_db),
):
    async with conn.cursor() as cur:
        await _can_view_ticket(cur, ticket_id, user)
        await cur.execute(
            "SELECT file_id, original_name, mime_type, size_bytes, sha256, retention_until, deleted_at, created_at "
            "FROM work_order_attachments WHERE work_order_id=%s ORDER BY id DESC",
            (ticket_id,),
        )
        rows = await cur.fetchall()
    return {"data": [
        {"file_id": row[0], "original_name": row[1], "mime_type": row[2], "size_bytes": int(row[3]),
         "sha256": row[4], "retention_until": _iso(row[5]), "deleted_at": _iso(row[6]),
         "created_at": _iso(row[7])} for row in rows
    ]}


@router.post("/tickets/{ticket_id}/attachments")
async def upload_attachment(
    ticket_id: int,
    request: Request,
    expected_version: int = Form(..., gt=0),
    file: UploadFile = File(...),
    user: dict = Depends(require_permission(WORKFLOW_ATTACHMENT_VIEW)),
    conn=Depends(get_workflow_db),
):
    async with conn.cursor() as cur:
        await _can_view_ticket(cur, ticket_id, user)
        await cur.execute(
            "SELECT COUNT(*) FROM work_order_attachments WHERE work_order_id=%s AND deleted_at IS NULL",
            (ticket_id,),
        )
        if int((await cur.fetchone())[0]) >= MAX_ATTACHMENTS_PER_TICKET:
            raise HTTPException(409, "每个工单最多保留 10 个附件")
    content = await file.read(20 * 1024 * 1024 + 1)
    try:
        saved = save_attachment(ticket_id, file.filename or "attachment", content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await _can_view_ticket(cur, ticket_id, user)
            await cur.execute(
                "SELECT id, version_no FROM work_orders WHERE id=%s FOR UPDATE",
                (ticket_id,),
            )
            ticket_row = await cur.fetchone()
            if not ticket_row:
                raise HTTPException(404, "工单不存在")
            if int(ticket_row[1]) != expected_version:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            await cur.execute(
                "SELECT COUNT(*) FROM work_order_attachments WHERE work_order_id=%s AND deleted_at IS NULL",
                (ticket_id,),
            )
            if int((await cur.fetchone())[0]) >= MAX_ATTACHMENTS_PER_TICKET:
                raise HTTPException(409, "每个工单最多保留 10 个附件")
            await cur.execute(
                "INSERT INTO work_order_attachments "
                "(work_order_id, file_id, original_name, storage_key, mime_type, sha256, size_bytes, uploaded_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (ticket_id, saved["file_id"], (file.filename or "附件")[:255], saved["storage_key"],
                 saved["mime_type"], saved["sha256"], saved["size_bytes"], user["id"]),
            )
            await cur.execute(
                "INSERT INTO work_order_events (work_order_id, event_type, actor_user_id, detail_json) "
                "VALUES (%s,'attachment_upload',%s,%s)",
                (ticket_id, user["id"], json.dumps({"file_id": saved["file_id"], "size": saved["size_bytes"]})),
            )
            await cur.execute(
                "UPDATE work_orders SET version_no=version_no+1 "
                "WHERE id=%s AND version_no=%s",
                (ticket_id, expected_version),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
        await conn.commit()
    except Exception:
        await conn.rollback()
        remove_attachment(saved["storage_key"])
        raise
    await record_admin_audit(
        user, "workflow.attachment.upload", target_type="work_order_attachment",
        target_name=saved["file_id"], detail={"ticket_id": ticket_id, "size": saved["size_bytes"]},
        **request_audit_fields(request),
    )
    return {"file_id": saved["file_id"], "sha256": saved["sha256"], "message": "附件已上传"}


@router.get("/tickets/{ticket_id}/attachments/{file_id}")
async def download_attachment(
    ticket_id: int,
    file_id: str,
    inline: bool = Query(default=False),
    user: dict = Depends(require_permission(WORKFLOW_ATTACHMENT_VIEW)),
    conn=Depends(get_workflow_db),
):
    if not SAFE_FILE_ID.fullmatch(file_id):
        raise HTTPException(422, "附件编号无效")
    async with conn.cursor() as cur:
        await _can_view_ticket(cur, ticket_id, user)
        await cur.execute(
            "SELECT original_name, storage_key, mime_type FROM work_order_attachments "
            "WHERE work_order_id=%s AND file_id=%s AND deleted_at IS NULL",
            (ticket_id, file_id),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "附件不存在或已过期删除")
    try:
        path = resolve_attachment(str(row[1]))
    except FileNotFoundError as exc:
        raise HTTPException(404, "附件文件不存在") from exc
    disposition = "inline" if inline else f'attachment; filename="{file_id}"'
    return FileResponse(path, media_type=row[2], filename=None if inline else str(row[0]), headers={"Content-Disposition": disposition})


@router.delete("/tickets/{ticket_id}/attachments/{file_id}")
async def delete_attachment(
    ticket_id: int,
    file_id: str,
    request: Request,
    expected_version: int = Query(..., gt=0),
    user: dict = Depends(require_permission(WORKFLOW_ATTACHMENT_VIEW)),
    conn=Depends(get_workflow_db),
):
    if not SAFE_FILE_ID.fullmatch(file_id):
        raise HTTPException(422, "附件编号无效")
    storage_key = None
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            ticket = await _can_view_ticket(cur, ticket_id, user)
            if int(ticket[4]) != expected_version:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            if user["id"] not in {ticket[0], ticket[1]} and not _can_manage(user):
                raise HTTPException(403, "只能删除本人参与工单的附件")
            await cur.execute(
                "SELECT storage_key FROM work_order_attachments WHERE work_order_id=%s AND file_id=%s AND deleted_at IS NULL FOR UPDATE",
                (ticket_id, file_id),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "附件不存在")
            storage_key = str(row[0])
            await cur.execute(
                "UPDATE work_order_attachments SET deleted_at=UTC_TIMESTAMP(), deleted_by=%s "
                "WHERE work_order_id=%s AND file_id=%s AND deleted_at IS NULL",
                (user["id"], ticket_id, file_id),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "附件状态已变化，请刷新后重试")
            await cur.execute(
                "UPDATE work_orders SET version_no=version_no+1 "
                "WHERE id=%s AND version_no=%s",
                (ticket_id, expected_version),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            await cur.execute(
                "INSERT INTO work_order_events (work_order_id, event_type, actor_user_id, detail_json) "
                "VALUES (%s,'attachment_delete',%s,%s)",
                (ticket_id, user["id"], json.dumps({"file_id": file_id})),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    if storage_key:
        remove_attachment(storage_key)
    await record_admin_audit(
        user, "workflow.attachment.delete", target_type="work_order_attachment",
        target_name=file_id, detail={"ticket_id": ticket_id}, **request_audit_fields(request),
    )
    return {"message": "附件已删除，元数据和删除记录已保留"}
