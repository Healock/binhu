"""统一外部来源目录、调度配置和安全运行历史。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import get_db
from deps import get_current_user, require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.external_acquisition_jobs import get_job, get_job_metadata, latest_job
from services.external_acquisition_registry import (
    SOURCE_ALIASES, SOURCE_DEFINITIONS, can_access_source, get_schedule, list_schedules,
    source_definition, update_schedule,
)
from services.external_acquisition_scheduler import run_registered_source
from services.maintenance import is_super_admin_user

router = APIRouter(prefix="/api/external-acquisition", tags=["外部来源调度"])


class ScheduleUpdate(BaseModel):
    enabled: bool = False
    cron_expr: str = Field(min_length=9, max_length=120)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(ge=1)


class RunRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


def _ensure_source_access(source_key: str, user: dict) -> None:
    # Keep the legacy residence boundary explicit: only a super administrator
    # may inspect or start the full scan, even though the registry also models
    # it as a source without a normal permission string.
    kind = source_key
    if kind.startswith("photo_sheet_"):
        raise HTTPException(410, "照片调取业务已下线，停止服务")
    if kind == "residence_full_scan" and not is_super_admin_user(user):
        raise HTTPException(403, "无权查看该外部获取任务")
    try:
        definition = source_definition(source_key)
    except ValueError as exc:
        raise HTTPException(404, "外部来源不存在") from exc
    if not can_access_source(user, definition):
        raise HTTPException(403, "当前权限组不能访问该外部来源")


def _ensure_known_source(source_key: str) -> None:
    try:
        source_definition(source_key)
    except ValueError as exc:
        raise HTTPException(404, "外部来源不存在") from exc


@router.get("/catalog")
async def external_catalog(user: dict = Depends(get_current_user), conn=Depends(get_db)):
    schedules = {item["source_key"]: item for item in await list_schedules(conn)}
    result = []
    for definition in SOURCE_DEFINITIONS:
        latest = await latest_job(definition.key)
        result.append({
            "source_key": definition.key, "name": definition.name,
            "category": definition.category, "description": definition.description,
            "permission": definition.permission,
            "accessible": can_access_source(user, definition),
            "supports_manual": definition.supports_manual,
            "supports_schedule": definition.supports_schedule,
            "supports_auto_commit": definition.supports_auto_commit,
            "params_schema": definition.params_schema,
            "schedule": schedules.get(definition.key), "latest_run": latest,
        })
    return {"data": result}


@router.get("/schedules")
async def external_schedules(_user: dict = Depends(get_current_user), conn=Depends(get_db)):
    return {"data": await list_schedules(conn)}


@router.put("/schedules/{source_key}")
async def save_external_schedule(source_key: str, payload: ScheduleUpdate, request: Request,
                                 user: dict = Depends(require_super_admin), conn=Depends(get_db)):
    try:
        definition = source_definition(source_key)
        if not definition.supports_schedule:
            raise ValueError("该来源不支持自动调度")
        result = await update_schedule(
            conn, source_key, enabled=payload.enabled, cron_expr=payload.cron_expr,
            timezone_name=payload.timezone, params=payload.params,
            revision=payload.revision, updated_by=int(user["id"]),
        )
    except RuntimeError as exc:
        if str(exc) == "schedule_revision_conflict":
            raise HTTPException(409, {"code": "schedule_revision_conflict", "message": "调度已被其他管理员修改，请重新读取"}) from exc
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid_schedule", "message": str(exc)}) from exc
    await record_admin_audit(
        user, "external_acquisition.schedule.update", target_type="external_schedule",
        target_name=source_key,
        detail={"enabled": payload.enabled, "cron_expr": payload.cron_expr,
                "timezone": payload.timezone, "revision": payload.revision},
        **request_audit_fields(request),
    )
    return {"data": result}


@router.post("/schedules/{source_key}/run", status_code=202)
async def run_external_schedule(source_key: str, payload: RunRequest, request: Request,
                                user: dict = Depends(get_current_user), conn=Depends(get_db)):
    _ensure_source_access(source_key, user)
    definition = source_definition(source_key)
    if not definition.supports_manual:
        raise HTTPException(409, "该来源仅展示监控状态，不支持手动运行")
    schedule = await get_schedule(conn, source_key)
    if not schedule:
        raise HTTPException(404, "调度记录不存在")
    try:
        run, reused = await run_registered_source(
            source_key, int(user["id"]), payload.params or {},
            schedule_id=int(schedule["id"]), trigger_source="manual",
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid_source_params", "message": str(exc)}) from exc
    await record_admin_audit(
        user, "external_acquisition.run", target_type="external_source",
        target_name=source_key, detail={"run_id": run.get("id"), "reused": reused},
        **request_audit_fields(request),
    )
    return {"data": run, "reused": reused}


@router.get("/schedules/{source_key}/runs")
async def external_schedule_runs(source_key: str, limit: int = Query(20, ge=1, le=100),
                                 user: dict = Depends(get_current_user)):
    # Run history is an aggregate status view.  Ordinary administrators may
    # inspect it even when they cannot trigger or edit the source.
    del user
    _ensure_known_source(source_key)
    from database import db_manager
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            canonical = SOURCE_ALIASES.get(source_key, source_key)
            kinds = [canonical, *[alias for alias, target in SOURCE_ALIASES.items() if target == canonical]]
            placeholders = ",".join(["%s"] * len(kinds))
            await cur.execute(
                f"SELECT id FROM _external_acquisition_runs WHERE kind IN ({placeholders}) "
                "ORDER BY id DESC LIMIT %s",
                (*kinds, limit),
            )
            ids = [int(row[0]) for row in await cur.fetchall()]
    runs = []
    for run_id in ids:
        run = await get_job(run_id)
        if run:
            runs.append(run)
    return {"data": runs}


@router.post("/runs/{run_id}/retry", status_code=202)
async def retry_external_run(run_id: int, request: Request, user: dict = Depends(get_current_user)):
    metadata = await get_job_metadata(run_id)
    if not metadata:
        raise HTTPException(404, "外部获取任务不存在")
    source_key = str(metadata["kind"])
    _ensure_source_access(source_key, user)
    try:
        run, reused = await run_registered_source(
            source_key, int(user["id"]), metadata.get("payload") or {},
            schedule_id=metadata.get("schedule_id"), trigger_source="retry",
        )
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid_source_params", "message": str(exc)}) from exc
    await record_admin_audit(
        user, "external_acquisition.run.retry", target_type="external_run",
        target_name=str(run_id), detail={"source_key": source_key,
                                         "new_run_id": run.get("id"), "reused": reused},
        **request_audit_fields(request),
    )
    return {"data": run, "reused": reused}


@router.get("/runs/{run_id}")
async def external_run(run_id: int, user: dict = Depends(get_current_user)):
    run = await get_job(run_id)
    if not run:
        raise HTTPException(404, "外部获取任务不存在")
    _ensure_source_access(str(run["kind"]), user)
    return run


@router.get("/latest")
async def external_latest(kind: str = Query(..., min_length=1, max_length=64), user: dict = Depends(get_current_user)):
    _ensure_source_access(kind, user)
    return {"data": await latest_job(kind)}
