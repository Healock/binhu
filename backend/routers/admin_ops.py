"""Super-administrator operations center APIs."""

import asyncio
import io
import json
import time
import zipfile

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from config import settings
from database import db_manager
from deps import require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.audit_display import (
    action_label,
    action_options,
    actor_account,
    actor_name,
    detail_items,
    result_label,
    target_display,
)
from services.backups import (
    create_backup_task,
    get_backup_schedule,
    list_backup_jobs,
    resolve_backup_file,
    update_backup_schedule,
)
from services.ops_client import get_container_logs
from services.ops_database import (
    get_database_overview,
    get_table_structure,
    list_database_tables,
)
from services.ops_overview import build_operations_overview
from services.ops_redaction import redact_text, sanitize_detail


router = APIRouter(prefix="/api/admin/ops", tags=["超级管理员运维中心"])
LOG_SOURCES = {
    "backend": "后端日志",
    "mysql": "MySQL 错误日志",
}


class BackupScheduleRequest(BaseModel):
    enabled: bool = True
    run_hour: int = Field(default=2, ge=0, le=23)
    run_minute: int = Field(default=0, ge=0, le=59)


class PasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)


def _require_log_source(source: str) -> str:
    if source not in LOG_SOURCES:
        raise HTTPException(status_code=404, detail="未知日志来源")
    return source


@router.get("/overview")
async def operations_overview(
    user: dict = Depends(require_super_admin),
):
    return await build_operations_overview()


@router.get("/logs/sources")
async def log_sources(user: dict = Depends(require_super_admin)):
    return {
        "data": [
            {"value": value, "label": label}
            for value, label in LOG_SOURCES.items()
        ]
    }


@router.get("/logs/stream")
async def stream_logs(
    source: str,
    request: Request,
    tail: int = Query(default=300, ge=1, le=2000),
    since_minutes: int = Query(default=15, ge=1, le=1440),
    user: dict = Depends(require_super_admin),
):
    source = _require_log_source(source)

    async def events():
        since = max(0, int(time.time()) - since_minutes * 60)
        first = True
        seen: set[str] = set()
        order: list[str] = []
        while True:
            if await request.is_disconnected():
                return
            try:
                result = await get_container_logs(
                    source,
                    tail=tail if first else 1000,
                    since=since,
                )
                first = False
                poll_time = int(time.time())
                sent = 0
                for line in result.get("lines", []):
                    message = redact_text(str(line.get("message", "")))
                    key = f"{line.get('stream')}:{message}"
                    if key in seen:
                        continue
                    seen.add(key)
                    order.append(key)
                    if len(order) > 5000:
                        seen.discard(order.pop(0))
                    payload = {
                        "source": source,
                        "stream": line.get("stream", "stdout"),
                        "message": message,
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    sent += 1
                since = max(since, poll_time - 1)
                if not sent:
                    yield ": keep-alive\n\n"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                payload = {
                    "message": "日志连接暂时不可用",
                    "detail": redact_text(str(exc))[:200],
                }
                yield (
                    "event: warning\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/logs/export")
async def export_logs(
    source: str,
    request: Request,
    since_minutes: int = Query(default=60, ge=1, le=1440),
    user: dict = Depends(require_super_admin),
):
    source = _require_log_source(source)
    result = await get_container_logs(
        source,
        tail=5000,
        since=max(0, int(time.time()) - since_minutes * 60),
    )
    text = "\n".join(
        redact_text(str(line.get("message", "")))
        for line in result.get("lines", [])
    )
    payload = text.encode("utf-8")
    if len(payload) > settings.LOG_EXPORT_MAX_BYTES:
        payload = payload[-settings.LOG_EXPORT_MAX_BYTES :]
        payload = payload.decode("utf-8", errors="ignore").encode("utf-8")
    await record_admin_audit(
        user,
        "logs.export",
        target_type="container",
        target_name=source,
        detail={"since_minutes": since_minutes, "bytes": len(payload)},
        **request_audit_fields(request),
    )
    return Response(
        content=payload,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="binhu-{source}-logs.txt"'
            )
        },
    )


@router.get("/databases")
async def databases(user: dict = Depends(require_super_admin)):
    return {"data": await get_database_overview()}


@router.get("/databases/{database_name}/tables")
async def database_tables(
    database_name: str,
    user: dict = Depends(require_super_admin),
):
    try:
        data = await list_database_tables(database_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"database": database_name, "data": data}


@router.get("/databases/{database_name}/tables/{table_name}")
async def table_structure(
    database_name: str,
    table_name: str,
    user: dict = Depends(require_super_admin),
):
    try:
        result = await get_table_structure(database_name, table_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="数据表不存在")
    return result


@router.get("/backups")
async def backups(
    limit: int = Query(default=50, ge=1, le=100),
    user: dict = Depends(require_super_admin),
):
    result = await list_backup_jobs(limit)
    result["schedule"] = await get_backup_schedule()
    return result


@router.post("/backups", status_code=202)
async def create_backup(
    request: Request,
    user: dict = Depends(require_super_admin),
):
    task_id, status, message = await create_backup_task("manual", user["id"])
    await record_admin_audit(
        user,
        "backup.create",
        target_type="backup",
        target_name=str(task_id) if task_id else "",
        result=status,
        **request_audit_fields(request),
    )
    if status == "conflict":
        raise HTTPException(status_code=409, detail=message)
    return {"task_id": task_id, "status": status, "message": message}


@router.get("/backup-schedule")
async def backup_schedule(user: dict = Depends(require_super_admin)):
    return await get_backup_schedule()


@router.put("/backup-schedule")
async def save_backup_schedule(
    payload: BackupScheduleRequest,
    request: Request,
    user: dict = Depends(require_super_admin),
):
    result = await update_backup_schedule(
        payload.enabled,
        payload.run_hour,
        payload.run_minute,
        user["id"],
    )
    await record_admin_audit(
        user,
        "backup.schedule.update",
        target_type="backup_schedule",
        target_name="daily",
        detail={
            "enabled": payload.enabled,
            "run_hour": payload.run_hour,
            "run_minute": payload.run_minute,
            "retention_days": 7,
        },
        **request_audit_fields(request),
    )
    return result


async def _verify_current_password(user_id: int, password: str) -> bool:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT password_hash FROM _users WHERE id=%s",
                (user_id,),
            )
            row = await cur.fetchone()
    finally:
        pool.release(conn)
    return bool(
        row
        and bcrypt.checkpw(
            password.encode("utf-8"),
            row[0].encode("utf-8"),
        )
    )


@router.post("/backups/{job_id}/download")
async def download_backup(
    job_id: int,
    payload: PasswordRequest,
    request: Request,
    user: dict = Depends(require_super_admin),
):
    audit_fields = request_audit_fields(request)
    if not await _verify_current_password(user["id"], payload.password):
        await record_admin_audit(
            user,
            "backup.download",
            target_type="backup",
            target_name=str(job_id),
            result="denied",
            **audit_fields,
        )
        raise HTTPException(status_code=403, detail="当前账号密码错误")
    resolved = await resolve_backup_file(job_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="备份文件不存在或已过期")
    path, filename = resolved
    await record_admin_audit(
        user,
        "backup.download",
        target_type="backup",
        target_name=str(job_id),
        detail={"filename": filename},
        **audit_fields,
    )
    return FileResponse(
        path,
        media_type="application/gzip",
        filename=filename,
    )


@router.get("/audit")
async def audit_log(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    action: str = Query(default="", max_length=80),
    user: dict = Depends(require_super_admin),
):
    offset = (page - 1) * page_size
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            where = "WHERE audit.action=%s" if action else ""
            params = (action,) if action else ()
            await cur.execute(
                f"SELECT COUNT(*) FROM _admin_audit_log AS audit {where}",
                params,
            )
            total = (await cur.fetchone())[0]
            await cur.execute(
                f"""
                SELECT audit.id, audit.user_id, audit.username, audit.action,
                       audit.target_type, audit.target_name,
                       COALESCE(sync_task.status, audit.result),
                       audit.detail_json, audit.ip_address, audit.user_agent,
                       audit.created_at, actor.display_name, member.name,
                       actor.username
                FROM _admin_audit_log AS audit
                LEFT JOIN _users AS actor ON actor.id=audit.user_id
                LEFT JOIN _grid_members AS member ON member.id=actor.member_id
                LEFT JOIN _sync_log AS sync_task
                  ON audit.action='sync.trigger'
                 AND audit.target_type='sync'
                 AND audit.target_name REGEXP '^[0-9]+$'
                 AND sync_task.id=CAST(audit.target_name AS UNSIGNED)
                {where}
                ORDER BY audit.id DESC
                LIMIT %s OFFSET %s
                """,
                (*params, page_size, offset),
            )
            rows = await cur.fetchall()
    finally:
        pool.release(conn)

    data = []
    for row in rows:
        try:
            detail = json.loads(row[7]) if isinstance(row[7], str) else row[7]
        except (TypeError, json.JSONDecodeError):
            detail = None
        resolved_actor_name = actor_name(
            member_name=row[12],
            display_name=row[11],
            current_username=row[13],
            recorded_username=row[2],
            user_id=row[1],
        )
        resolved_actor_account = actor_account(row[13], row[2])
        data.append(
            {
                "id": row[0],
                "user_id": row[1],
                "username": row[2],
                "actor_name": resolved_actor_name,
                "actor_account": resolved_actor_account,
                "action": row[3],
                "action_label": action_label(row[3]),
                "target_type": row[4],
                "target_name": row[5],
                "target_display": target_display(row[4], row[5]),
                "result": row[6],
                "result_label": result_label(row[6]),
                "detail": detail,
                "detail_items": detail_items(detail),
                "ip_address": row[8],
                "user_agent": row[9],
                "created_at": row[10].isoformat() + "Z",
            }
        )
    return {
        "data": data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "action_options": action_options(),
    }


@router.post("/diagnostics")
async def diagnostic_package(
    request: Request,
    user: dict = Depends(require_super_admin),
):
    overview = await build_operations_overview()
    backup_data = await list_backup_jobs(20)
    logs = {}
    for source in LOG_SOURCES:
        try:
            result = await get_container_logs(source, tail=500)
            logs[source] = "\n".join(
                redact_text(str(line.get("message", "")))
                for line in result.get("lines", [])
            )
        except Exception:
            logs[source] = "日志暂时不可用"

    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(
            "overview.json",
            json.dumps(
                sanitize_detail(overview),
                ensure_ascii=False,
                indent=2,
            ),
        )
        archive.writestr(
            "backups.json",
            json.dumps(
                sanitize_detail(backup_data),
                ensure_ascii=False,
                indent=2,
            ),
        )
        for source, content in logs.items():
            archive.writestr(f"{source}.log", content)
        archive.writestr(
            "README.txt",
            "本诊断包已排除业务数据、密码、令牌和 Cookie。\n",
        )
    buffer.seek(0)
    await record_admin_audit(
        user,
        "diagnostics.export",
        target_type="system",
        target_name="operations-center",
        **request_audit_fields(request),
    )
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                'attachment; filename="binhu-diagnostics.zip"'
            )
        },
    )
