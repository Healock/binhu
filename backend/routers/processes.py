"""PR-B1 独立流程中心 API。

本轮只提供后端定义、发布和运行时接口；表单渲染、流程配置页面与待办页面属于 B2。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from deps import get_current_user
from routers.workflow import get_workflow_db
from services.permissions import (
    WORKFLOW_CONFIG_MANAGE,
    WORKFLOW_TICKET_CREATE,
    WORKFLOW_TICKET_HANDLE,
    WORKFLOW_TICKET_VIEW,
)
from deps import require_permission
from services.process_engine import ProcessEngine, ProcessValidationError, validate_nodes


router = APIRouter(prefix="/api/processes", tags=["流程引擎"])


class ProcessDefinitionPayload(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=80)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    form_definition_id: int | None = Field(default=None, gt=0)
    module_id: str = Field(default="", max_length=80)
    default_due_hours: int | None = Field(default=None, ge=1, le=8760)


class FormDefinitionPayload(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=80)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


class FormVersionPayload(BaseModel):
    form_schema: dict[str, Any]


class ProcessVersionPayload(BaseModel):
    nodes: list[dict[str, Any]] = Field(min_length=1, max_length=100)


class ProcessStartPayload(BaseModel):
    process_version_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    form_data: dict[str, Any] = Field(default_factory=dict)


class ProcessDecisionPayload(BaseModel):
    decision: str = Field(pattern=r"^(approve|complete|reject|return)$")
    note: str = Field(default="", max_length=2000)


class ProcessResubmitPayload(BaseModel):
    form_data: dict[str, Any] = Field(default_factory=dict)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProcessValidationError):
        return HTTPException(422, str(exc))
    return HTTPException(500, "流程处理失败")


@router.post("/forms")
async def create_form_definition(payload: FormDefinitionPayload,
                                conn=Depends(get_workflow_db),
                                user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE))):
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            form_id = await ProcessEngine(cur).create_form(
                code=payload.code, name=payload.name, description=payload.description,
                created_by=int(user["id"]),
            )
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        raise HTTPException(409, "表单定义保存失败") from exc
    return {"id": form_id, "code": payload.code}


@router.post("/forms/{form_definition_id}/versions")
async def publish_form_version(form_definition_id: int, payload: FormVersionPayload,
                               conn=Depends(get_workflow_db),
                               user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE))):
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            version_id = await ProcessEngine(cur).publish_form(
                form_definition_id, payload.form_schema, int(user["id"]),
            )
        await conn.commit()
    except ProcessValidationError as exc:
        await conn.rollback()
        raise _error(exc) from exc
    except Exception as exc:
        await conn.rollback()
        raise HTTPException(409, "表单版本发布失败") from exc
    return {"id": version_id, "form_definition_id": form_definition_id, "status": "published"}


@router.get("/definitions")
async def list_process_definitions(conn=Depends(get_workflow_db),
                                  user: dict = Depends(require_permission(WORKFLOW_TICKET_VIEW))):
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id,code,name,description,form_definition_id,module_id,default_due_hours,enabled,current_version_id "
            "FROM process_definitions ORDER BY id"
        )
        rows = await cur.fetchall()
    return {"data": [
        {"id": int(row[0]), "code": row[1], "name": row[2], "description": row[3],
         "form_definition_id": row[4], "module_id": row[5], "default_due_hours": row[6],
         "enabled": bool(row[7]), "current_version_id": row[8]}
        for row in rows
    ]}


@router.post("/definitions")
async def create_process_definition(payload: ProcessDefinitionPayload,
                                    conn=Depends(get_workflow_db),
                                    user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE))):
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            engine = ProcessEngine(cur)
            process_id = await engine.create_process(
                code=payload.code, name=payload.name, description=payload.description,
                form_definition_id=payload.form_definition_id, module_id=payload.module_id,
                default_due_hours=payload.default_due_hours, created_by=int(user["id"]),
            )
        await conn.commit()
    except ProcessValidationError as exc:
        await conn.rollback()
        raise _error(exc) from exc
    except Exception as exc:
        await conn.rollback()
        raise HTTPException(409, "流程标识已存在或流程定义保存失败") from exc
    return {"id": process_id, "code": payload.code}


@router.post("/definitions/{process_definition_id}/versions")
async def publish_process_version(process_definition_id: int, payload: ProcessVersionPayload,
                                  conn=Depends(get_workflow_db),
                                  user: dict = Depends(require_permission(WORKFLOW_CONFIG_MANAGE))):
    try:
        await conn.begin()
        validate_nodes(payload.nodes)
        async with conn.cursor() as cur:
            version_id = await ProcessEngine(cur).publish_process(
                process_definition_id, payload.nodes, int(user["id"]),
            )
        await conn.commit()
    except ProcessValidationError as exc:
        await conn.rollback()
        raise _error(exc) from exc
    except Exception as exc:
        await conn.rollback()
        raise HTTPException(409, "流程版本发布失败") from exc
    return {"id": version_id, "process_definition_id": process_definition_id, "status": "published"}


@router.post("/instances")
async def start_process(payload: ProcessStartPayload, conn=Depends(get_workflow_db),
                        user: dict = Depends(require_permission(WORKFLOW_TICKET_CREATE))):
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            result = await ProcessEngine(cur).start(
                process_version_id=payload.process_version_id, requester_user_id=int(user["id"]),
                title=payload.title, description=payload.description, form_data=payload.form_data,
            )
        await conn.commit()
    except ProcessValidationError as exc:
        await conn.rollback()
        raise _error(exc) from exc
    except Exception as exc:
        await conn.rollback()
        raise HTTPException(409, "流程实例创建失败") from exc
    return result


@router.post("/tasks/{task_id}/decision")
async def decide_process_task(task_id: int, payload: ProcessDecisionPayload,
                              conn=Depends(get_workflow_db),
                              user: dict = Depends(require_permission(WORKFLOW_TICKET_HANDLE))):
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            result = await ProcessEngine(cur).decide(
                task_id=task_id, actor_user_id=int(user["id"]), decision=payload.decision,
                note=payload.note,
            )
        await conn.commit()
    except ProcessValidationError as exc:
        await conn.rollback()
        raise _error(exc) from exc
    except Exception as exc:
        await conn.rollback()
        raise HTTPException(409, "流程任务处理失败") from exc
    return result


@router.post("/instances/{instance_id}/resubmit")
async def resubmit_process(instance_id: int, payload: ProcessResubmitPayload,
                           conn=Depends(get_workflow_db),
                           user: dict = Depends(require_permission(WORKFLOW_TICKET_CREATE))):
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            result = await ProcessEngine(cur).resubmit(
                instance_id=instance_id, requester_user_id=int(user["id"]), form_data=payload.form_data,
            )
        await conn.commit()
    except ProcessValidationError as exc:
        await conn.rollback()
        raise _error(exc) from exc
    except Exception as exc:
        await conn.rollback()
        raise HTTPException(409, "流程重提失败") from exc
    return result


@router.post("/maintenance/timeouts")
async def advance_process_timeouts(conn=Depends(get_workflow_db), user: dict = Depends(get_current_user)):
    if user.get("role") not in {"admin", "super_admin"}:
        raise HTTPException(403, "仅管理员可以推进流程超时")
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            changed = await ProcessEngine(cur).advance_timeouts()
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        raise HTTPException(500, "流程超时推进失败") from exc
    return {"changed": changed}
