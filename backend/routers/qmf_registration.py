"""全民防模型三单条预演与封闭登记 API。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from config import settings
from database import db_manager, get_db
from deps import require_permission, require_super_admin
from routers.mobile_tasks import _mobile_task_detail_data
from routers.query import _writeback_enabled, update_source_fields
from services.audit import record_admin_audit, request_audit_fields
from services.administrative_areas import resolve_identity_area
from services.online_source import source_row_hash
from services.permissions import ONLINE_RAW_VIEW, QMF_REGISTRATION_EXECUTE
from services.qmf_community import resolve_qmf_community
from services.qmf_config import (
    QMF_CONFIG_KEYS,
    load_qmf_config,
    public_config,
    serialize_value,
    settings_config,
)
from services.qmf_registration import (
    MODEL_THREE_PARSER,
    QmfCollectedContext,
    QmfPreviewError,
    RESULT_LEAVE_NOT_RETURNING,
    normalize_qmf_result,
    normalize_identity,
    preview_configured,
    qmf_operation_busy,
    registration_capability,
    run_guarded_registration,
    run_guarded_preview,
    valid_identity,
)
from services.qmf_runs import (
    ALL_RUN_STEPS,
    PREPARE_TTL_SECONDS,
    TENCENT_MARKER,
    WRITE_STEP_KEYS,
    initial_steps,
    parse_steps,
    serialize_steps,
    steps_for_result,
    utc_text,
)
from services.qmf_status import (
    QmfLegacyStatus,
    QmfLegacyStatusClient,
    STATUS_COMPLETED_MATCH,
    STATUS_COMPLETED_MISMATCH,
    ensure_registration_allowed,
)
from services.qmf_status_scan import (
    create_status_scan_run,
    latest_status_scan_payload,
    status_scan_payload,
    valid_schedule_time,
)


router = APIRouter(prefix="/api/qmf-registration", tags=["全民防模型三封闭测试"])
_QMF_CLAIM_LOCK = "binhu:qmf-registration:claim"
_QMF_SAFE_TRANSPORT_ERRORS = frozenset({
    "read_timeout",
    "write_timeout",
    "connect_timeout",
    "connect_error",
    "connection_error",
    "incomplete_read",
    "request_error",
})


class QmfPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parser_type: str = Field(min_length=1, max_length=100)
    row_key: str = Field(min_length=1, max_length=190)
    source_id: int = Field(gt=0)
    expected_revision: int = Field(ge=0)


class QmfConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_enabled: bool | None = None
    registration_enabled: bool = False
    login_protocol_verified: bool | None = None
    write_protocol_verified: bool | None = None
    api_base_url: str = Field(default="", max_length=500)
    login_host: str = Field(default="", max_length=255)
    login_port: int = Field(default=0, ge=0, le=65535)
    source_username: str = Field(default="", max_length=200)
    source_password: str | None = Field(default=None, max_length=500)
    source_imei: str = Field(default="", max_length=200)
    source_machine_uid: str = Field(default="", max_length=200)
    expected_station_code: str = Field(default="320584710000", max_length=100)
    expected_station_name: str = Field(default="滨湖新城派出所", max_length=200)
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    session_max_seconds: int = Field(default=45, ge=1, le=120)
    status_scan_enabled: bool = False
    status_scan_time: str = Field(default="", max_length=5)


class QmfExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _safe_error_detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


async def _stored_qmf_keys(conn) -> set[str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT config_key FROM _system_config WHERE config_key LIKE 'qmf_%'"
        )
        return {str(row[0]) for row in await cur.fetchall()}


@router.get("/config")
async def get_qmf_config(
    _user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    config = await load_qmf_config(conn)
    return public_config(config, await _stored_qmf_keys(conn))


@router.put("/config")
async def update_qmf_config(
    data: QmfConfigUpdate,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    current = await load_qmf_config(conn)
    if (
        data.expected_station_code.strip() != "320584710000"
        or data.expected_station_name.strip() != "滨湖新城派出所"
    ):
        raise HTTPException(400, "全民防只读预演目标固定为滨湖新城派出所")
    if data.status_scan_enabled and not valid_schedule_time(data.status_scan_time):
        raise HTTPException(400, "开启每日扫描前请选择有效的执行时间")
    if data.status_scan_time and not valid_schedule_time(data.status_scan_time):
        raise HTTPException(400, "每日扫描时间格式应为 HH:mm")

    values: dict[str, Any] = {
        "qmf_registration_enabled": "1" if data.registration_enabled else "0",
        "qmf_api_base_url": data.api_base_url.strip(),
        "qmf_login_host": data.login_host.strip(),
        "qmf_login_port": str(data.login_port),
        "qmf_source_username": data.source_username.strip(),
        "qmf_source_imei": data.source_imei.strip(),
        "qmf_source_machine_uid": data.source_machine_uid.strip(),
        "qmf_expected_station_code": data.expected_station_code.strip(),
        "qmf_expected_station_name": data.expected_station_name.strip(),
        "qmf_timeout_seconds": str(data.timeout_seconds),
        "qmf_session_max_seconds": str(data.session_max_seconds),
        "qmf_status_scan_enabled": "1" if data.status_scan_enabled else "0",
        "qmf_status_scan_time": data.status_scan_time.strip(),
    }
    if data.source_password is not None:
        values["qmf_source_password"] = data.source_password

    if data.registration_enabled:
        password = (
            data.source_password
            if data.source_password is not None
            else current.source_password
        )
        required = (
            values["qmf_api_base_url"],
            values["qmf_login_host"],
            values["qmf_login_port"],
            values["qmf_source_username"],
            password,
            values["qmf_source_imei"],
            values["qmf_source_machine_uid"],
            values["qmf_expected_station_code"],
            values["qmf_expected_station_name"],
        )
        if not all(required):
            raise HTTPException(400, "开启全民防功能前请先完整填写接口、账号和设备信息")

    async with conn.cursor() as cur:
        for key, value in values.items():
            if key not in QMF_CONFIG_KEYS:
                continue
            stored = serialize_value(key, value)
            await cur.execute(
                "INSERT INTO _system_config (config_key, config_value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE config_value = %s",
                (key, stored, stored),
            )
    changed_keys = sorted(values)
    await record_admin_audit(
        user,
        "qmf_registration.config.update",
        target_type="system_config",
        target_name="qmf_closed_test",
        detail={"keys": changed_keys},
        **request_audit_fields(request),
    )
    config = await load_qmf_config(conn)
    return public_config(config, await _stored_qmf_keys(conn))


@router.post("/status-scans", status_code=202)
async def start_qmf_status_scan(
    request: Request,
    user: dict = Depends(require_permission(QMF_REGISTRATION_EXECUTE)),
):
    try:
        run_id, total = await create_status_scan_run(
            trigger_source="manual",
            requested_by=int(user["id"]),
        )
    except RuntimeError as exc:
        if str(exc) == "scan_busy":
            raise HTTPException(409, "已有全民防反馈扫描正在运行") from exc
        raise
    await record_admin_audit(
        user,
        "qmf_status_scan.start",
        target_type="qmf_status_scan",
        target_name=str(run_id),
        detail={"run_id": run_id, "target_count": total, "mode": "full"},
        **request_audit_fields(request),
    )
    return {"data": await status_scan_payload(run_id)}


@router.get("/status-scans/latest")
async def get_latest_qmf_status_scan(
    _user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
):
    return {"data": await latest_status_scan_payload()}


@router.get("/status-scans/{run_id}")
async def get_qmf_status_scan(
    run_id: int,
    _user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
):
    payload = await status_scan_payload(run_id)
    if not payload:
        raise HTTPException(404, "全民防反馈扫描不存在")
    return {"data": payload}


async def _record_preview_audit(
    *,
    request: Request,
    user: dict,
    source_id: int,
    result: str,
    started_at: float,
    error_code: str = "",
    error_step: str = "",
    upstream_status: int | None = None,
    transport_error: str = "",
    photo: dict[str, Any] | None = None,
) -> None:
    detail: dict[str, Any] = {
        "source_id": int(source_id),
        "mode": "read_only",
        "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
        "result_code": error_code or "success",
    }
    if error_step:
        detail["error_step"] = error_step[:64]
    if upstream_status is not None:
        detail["upstream_http_status"] = int(upstream_status)
    if transport_error in _QMF_SAFE_TRANSPORT_ERRORS:
        detail["transport_error"] = transport_error
    if photo:
        detail["photo"] = {
            "mime_type": str(photo.get("mime_type") or "")[:50],
            "size_bytes": int(photo.get("size_bytes") or 0),
            "sha256": str(photo.get("sha256") or "")[:64],
        }
    await record_admin_audit(
        user,
        "qmf_registration.preview",
        target_type="online_source_row",
        target_name=str(source_id),
        result=result,
        detail=detail,
        **request_audit_fields(request),
    )


async def _assert_source_unchanged(
    conn,
    *,
    parser_type: str,
    row_key: str,
    source_id: int,
    expected_revision: int,
    expected_hash: str,
) -> None:
    row = None
    for attempt in range(3):
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT source.revision, source.row_hash,
                       projection.source_count, projection.conflict
                FROM _online_source_rows AS source
                LEFT JOIN _online_source_projection AS projection
                  ON projection.parser_type=source.parser_type
                 AND projection.row_key=source.row_key
                WHERE source.id=%s AND source.parser_type=%s AND source.row_key=%s
                """,
                (source_id, parser_type, row_key),
            )
            row = await cur.fetchone()
        if not row:
            raise QmfPreviewError(
                "source_missing", "腾讯来源行已不存在，请刷新后重试", 409
            )
        if row[2] is not None:
            break
        # Older sync workers may briefly expose the source row before their
        # delete-and-insert projection refresh finishes.  This is a local,
        # pre-write consistency recheck only; no upstream or write request is
        # retried, and the source revision/hash still must match below.
        if attempt < 2:
            await asyncio.sleep(0.05)
    if row[2] is None:
        raise QmfPreviewError(
            "source_projection_refreshing",
            "任务来源正在刷新，请稍后重新核对",
            503,
        )
    if int(row[0]) != expected_revision or str(row[1]) != expected_hash:
        raise QmfPreviewError("source_changed", "腾讯来源行已变化，请刷新后重试", 409)
    if int(row[2] or 0) != 1 or bool(row[3]):
        raise QmfPreviewError("source_not_unique", "任务来源状态已变化，请刷新后重试", 409)


def _safe_digest(*parts: Any) -> str:
    payload = "\x1f".join(str(part or "") for part in parts).encode("utf-8")
    return hmac.new(
        str(settings.ENCRYPTION_KEY).encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


async def _eligible_platform_task(
    data: QmfPreviewRequest,
    *,
    user: dict,
    conn,
    resolve_registration_fields: bool = True,
) -> tuple[dict[str, Any], str]:
    if data.parser_type != MODEL_THREE_PARSER:
        raise QmfPreviewError(
            "parser_not_supported", "第一版仅支持疑似未注销模型三", 422
        )
    detail = await _mobile_task_detail_data(
        data.parser_type,
        data.row_key,
        user,
        conn,
        include_photo_requests=False,
    )
    sources = detail.get("sources") or []
    if detail.get("task", {}).get("conflict") or len(sources) != 1:
        raise QmfPreviewError("source_not_unique", "该任务不是唯一有效来源", 409)
    source = sources[0]
    if int(source.get("id") or 0) != data.source_id:
        raise QmfPreviewError(
            "source_mismatch", "腾讯来源行已变化，请刷新后重试", 409
        )
    if int(source.get("revision") or 0) != data.expected_revision:
        raise QmfPreviewError(
            "source_revision_conflict", "腾讯来源版本已变化，请刷新后重试", 409
        )
    values = source.get("values") or {}
    current_hash = source_row_hash(values)
    if current_hash != str(source.get("row_hash") or ""):
        raise QmfPreviewError("source_hash_conflict", "腾讯来源摘要校验失败", 409)
    result = normalize_qmf_result(values.get("核查结果"))
    if not result:
        raise QmfPreviewError(
            "result_not_supported", "当前仅支持“在吴、近期返吴、离开不返吴”三种结果", 422
        )
    identity = normalize_identity(values.get("身份证号"))
    if not valid_identity(identity):
        raise QmfPreviewError("identity_invalid", "任务身份证号格式无效", 422)
    name = str(values.get("姓名") or "").strip()
    if not name:
        raise QmfPreviewError("name_missing", "任务姓名为空，不能预演", 422)
    platform_task = {
        "parser_type": data.parser_type,
        "row_key": data.row_key,
        "source_id": data.source_id,
        "name": name,
        "identity_number": identity,
        "phone": str(values.get("联系方式") or "").strip(),
        "address": str(values.get("地址") or "").strip(),
        "community": str(values.get("下发社区") or "").strip(),
        "result": result,
    }
    if result == RESULT_LEAVE_NOT_RETURNING and resolve_registration_fields:
        async with conn.cursor() as cur:
            area = await resolve_identity_area(cur, identity)
            if area is None or not area.full_name:
                raise QmfPreviewError(
                    "destination_area_missing",
                    "身份证前六位未找到对应户籍行政区划",
                    409,
                )
            try:
                community = await resolve_qmf_community(
                    cur,
                    source_community=platform_task["community"],
                    address=platform_task["address"],
                )
            except ValueError as exc:
                messages = {
                    "no_enabled_community": "平台没有可用社区，不能反馈离开不返吴",
                    "community_ambiguous": "任务地址同时匹配多个社区，请先完善小区管理",
                    "community_conflict": "任务下发社区与小区地址匹配结果不一致",
                    "community_not_found": "无法从下发社区或小区管理唯一确定社区",
                    "community_code_missing": "对应社区尚未填写10位全民防社区代码",
                }
                code = str(exc)
                raise QmfPreviewError(
                    code, messages.get(code, "社区映射失败"), 409
                ) from exc
        platform_task.update({
            "resolved_community": community.name,
            "qmf_community_code": community.qmf_community_code,
            # qwdxzqh is the raw six-digit identity prefix.  The Chinese
            # administrative-division text is submitted separately as qwdxz.
            "destination_code": identity[:6],
            "destination_address": area.full_name,
        })
    return platform_task, current_hash


async def _legacy_status_for_task(
    conn,
    *,
    platform_task: dict[str, Any],
    source_id: int,
    client: QmfLegacyStatusClient | None = None,
) -> QmfLegacyStatus:
    status = await (client or QmfLegacyStatusClient()).query(
        identity=str(platform_task.get("identity_number") or ""),
        expected_result=str(platform_task.get("result") or ""),
    )
    if status.state not in {STATUS_COMPLETED_MATCH, STATUS_COMPLETED_MISMATCH}:
        return status
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM _qmf_registration_runs "
            "WHERE parser_type=%s AND source_id=%s AND status='succeeded' "
            "LIMIT 1",
            (str(platform_task.get("parser_type") or ""), int(source_id)),
        )
        locally_completed = bool(await cur.fetchone())
    return QmfLegacyStatus(
        **{
            **status.public_payload(),
            "origin": (
                "binhu_automatic" if locally_completed else "legacy_manual_or_other"
            ),
        }
    )


_RUN_SELECT = """
    SELECT id, parser_type, source_id, expected_revision,
           expected_row_hash, requested_by, status, steps_json, result_code,
           photo_sha256, photo_mime_type, photo_size_bytes,
           tencent_marker_status, tencent_marker_error,
           prepared_at, expires_at, execution_started_at, completed_at,
           created_at, updated_at
    FROM _qmf_registration_runs
    WHERE id=%s
"""


def _run_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    status = str(row[6] or "")
    marker_status = str(row[12] or "not_started")
    steps = parse_steps(row[7])
    write_progress = any(
        item["key"] in WRITE_STEP_KEYS
        and item["status"] in {"sending", "succeeded", "uncertain"}
        for item in steps
    )
    return {
        "id": int(row[0]),
        "parser_type": str(row[1] or ""),
        "source_id": int(row[2]),
        "expected_revision": int(row[3]),
        "_expected_row_hash": str(row[4] or ""),
        "_requested_by": int(row[5]),
        "status": status,
        "steps": steps,
        "result_code": str(row[8] or ""),
        "photo": {
            "sha256": str(row[9] or ""),
            "mime_type": str(row[10] or ""),
            "size_bytes": int(row[11] or 0),
        },
        "tencent_marker_status": marker_status,
        "tencent_marker_error": str(row[13] or ""),
        "prepared_at": utc_text(row[14]),
        "expires_at": utc_text(row[15]),
        "execution_started_at": utc_text(row[16]),
        "completed_at": utc_text(row[17]),
        "created_at": utc_text(row[18]),
        "updated_at": utc_text(row[19]),
        "can_execute": status == "prepared",
        # A pre-v0.21.3 worker could persist a local pre-write interruption as
        # ``uncertain`` even when every external write step was still pending.
        # That state is recoverable because no external side effect could have
        # started; once any write step has progress, it remains frozen.
        "can_reprepare": status in {"failed", "uncertain"} and not write_progress,
        "can_retry_marker": status == "succeeded" and marker_status in {
            "not_started", "pending", "conflict", "failed",
        },
    }


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in run.items() if not key.startswith("_")}


async def _load_run(conn, run_id: int, *, user_id: int | None = None) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE _qmf_registration_runs "
            "SET status='expired', result_code='prepare_expired' "
            "WHERE id=%s AND status='prepared' AND expires_at<=UTC_TIMESTAMP()",
            (run_id,),
        )
        await cur.execute(_RUN_SELECT, (run_id,))
        row = await cur.fetchone()
    if not row or (user_id is not None and int(row[5]) != int(user_id)):
        raise HTTPException(404, "全民防登记准备记录不存在")
    return _run_payload(row)


async def _create_prepared_run(
    conn,
    *,
    data: QmfPreviewRequest,
    user: dict,
    expected_hash: str,
    preview: dict[str, Any],
) -> dict[str, Any]:
    upstream_task = preview.get("upstream_task") or {}
    photo = preview.get("photo") or {}
    upstream_digest = _safe_digest("qmf-task", upstream_task.get("task_id"))
    row_key_digest = _safe_digest("qmf-row", data.parser_type, data.row_key)
    idempotency_key = _safe_digest(
        "qmf-run", data.parser_type, data.row_key, data.source_id, expected_hash,
        upstream_digest,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, status, steps_json FROM _qmf_registration_runs "
            "WHERE (row_key_digest=%s OR upstream_task_digest=%s "
            "OR idempotency_key=%s) "
            "AND status IN ('executing','succeeded','uncertain','failed') "
            "ORDER BY id DESC LIMIT 1",
            (row_key_digest, upstream_digest, idempotency_key),
        )
        attempted = await cur.fetchone()
        if attempted:
            if str(attempted[1]) == "succeeded":
                raise QmfPreviewError(
                    "registration_already_succeeded",
                    "该任务已有全民防成功反馈记录，不能重复登记",
                    409,
                )
            attempted_steps = parse_steps(attempted[2])
            write_progress = any(
                item["key"] in WRITE_STEP_KEYS
                and item["status"] in {"sending", "succeeded", "uncertain"}
                for item in attempted_steps
            )
            if str(attempted[1]) in {"failed", "uncertain"} and not write_progress:
                await cur.execute(
                    "UPDATE _qmf_registration_runs "
                    "SET status='superseded', "
                    "result_code='manual_reprepare_after_prewrite_failure' "
                    "WHERE id=%s AND status IN ('failed','uncertain')",
                    (int(attempted[0]),),
                )
            else:
                raise QmfPreviewError(
                    "registration_frozen",
                    "该任务已有真实登记执行记录，已冻结重复执行，请先人工核查",
                    409,
                )
        await cur.execute(
            "UPDATE _qmf_registration_runs "
            "SET status='superseded', result_code='new_prepare_created' "
            "WHERE parser_type=%s AND source_id=%s "
            "AND requested_by=%s AND status='prepared'",
            (data.parser_type, data.source_id, user["id"]),
        )
        await cur.execute(
            f"""
            INSERT INTO _qmf_registration_runs (
                parser_type, row_key_digest, source_id, expected_revision,
                expected_row_hash, idempotency_key, requested_by, status,
                steps_json, upstream_task_digest, photo_sha256,
                photo_mime_type, photo_size_bytes, expires_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,'prepared',%s,%s,%s,%s,%s,
                DATE_ADD(UTC_TIMESTAMP(), INTERVAL {PREPARE_TTL_SECONDS} SECOND)
            )
            """,
            (
                data.parser_type,
                row_key_digest,
                data.source_id,
                data.expected_revision,
                expected_hash,
                idempotency_key,
                user["id"],
                serialize_steps(initial_steps(
                    normalize_qmf_result(
                        (preview.get("platform_task") or {}).get("result")
                    )
                )),
                upstream_digest,
                str(photo.get("sha256") or "")[:64],
                str(photo.get("mime_type") or "")[:50],
                int(photo.get("size_bytes") or 0),
            ),
        )
        run_id = int(cur.lastrowid)
    return await _load_run(conn, run_id, user_id=int(user["id"]))


async def _update_run_step(
    conn,
    run_id: int,
    key: str,
    status: str,
    result_code: str = "",
) -> None:
    if key not in ALL_RUN_STEPS:
        raise RuntimeError("unknown qmf registration step")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT steps_json FROM _qmf_registration_runs WHERE id=%s",
            (run_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise RuntimeError("qmf registration run missing")
        steps = parse_steps(row[0])
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        found = False
        for item in steps:
            if item["key"] != key:
                continue
            found = True
            item["status"] = status
            item["result_code"] = result_code[:64]
            if status == "sending" and not item.get("started_at"):
                item["started_at"] = now
            if status in {"succeeded", "failed", "uncertain"}:
                item["finished_at"] = now
            break
        if not found:
            steps.append({
                "key": key,
                "label": ALL_RUN_STEPS[key],
                "status": status,
                "result_code": result_code[:64],
                "started_at": now if status == "sending" else None,
                "finished_at": now if status in {"succeeded", "failed", "uncertain"} else None,
            })
        await cur.execute(
            "UPDATE _qmf_registration_runs SET steps_json=%s WHERE id=%s",
            (serialize_steps(steps), run_id),
        )


async def _finish_sending_step(
    conn,
    run_id: int,
    *,
    status: str,
    result_code: str,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT steps_json FROM _qmf_registration_runs WHERE id=%s",
            (run_id,),
        )
        row = await cur.fetchone()
    if not row:
        return
    steps = parse_steps(row[0])
    sending = next((item for item in reversed(steps) if item["status"] == "sending"), None)
    if sending:
        await _update_run_step(conn, run_id, sending["key"], status, result_code)


async def _online_writeback_available(conn) -> bool:
    async with conn.cursor() as cur:
        return bool(await _writeback_enabled(cur))


async def _database_registration_active(conn) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM _qmf_registration_runs "
            "WHERE status='executing' LIMIT 1"
        )
        return bool(await cur.fetchone())


async def _claim_run(
    conn,
    run_id: int,
    *,
    user: dict,
    config,
) -> dict[str, Any]:
    if qmf_operation_busy():
        raise HTTPException(429, "已有一条全民防任务正在执行")
    if not config.registration_configured:
        raise HTTPException(503, "全民防真实登记尚未完成安全配置")
    if not await _online_writeback_available(conn):
        raise HTTPException(503, "在线回写已暂停，不能开始全民防真实登记")
    lock_acquired = False
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT GET_LOCK(%s, 2)", (_QMF_CLAIM_LOCK,))
            lock_row = await cur.fetchone()
            if not lock_row or int(lock_row[0] or 0) != 1:
                raise HTTPException(429, "全民防登记领取繁忙，请稍后重试")
            lock_acquired = True
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(_RUN_SELECT + " FOR UPDATE", (run_id,))
            row = await cur.fetchone()
            if not row or int(row[5]) != int(user["id"]):
                raise HTTPException(404, "全民防登记准备记录不存在")
            if str(row[6]) != "prepared":
                raise HTTPException(409, "该登记准备记录已执行、已失效或已被冻结")
            await cur.execute(
                "SELECT id FROM _qmf_registration_runs "
                "WHERE status='executing' AND id<>%s LIMIT 1 FOR UPDATE",
                (run_id,),
            )
            if await cur.fetchone():
                raise HTTPException(429, "已有一条全民防任务正在执行")
            await cur.execute(
                "SELECT prior.id, prior.status "
                "FROM _qmf_registration_runs AS current_run "
                "JOIN _qmf_registration_runs AS prior "
                "  ON prior.id<>current_run.id "
                " AND (prior.row_key_digest=current_run.row_key_digest "
                "      OR prior.upstream_task_digest=current_run.upstream_task_digest "
                "      OR prior.idempotency_key=current_run.idempotency_key) "
                "WHERE current_run.id=%s "
                "AND prior.status IN ('executing','succeeded','uncertain','failed') "
                "ORDER BY prior.id DESC LIMIT 1 FOR UPDATE",
                (run_id,),
            )
            if await cur.fetchone():
                raise HTTPException(
                    409,
                    "该任务已有真实登记执行记录，已冻结重复执行，请先人工核查",
                )
            await cur.execute(
                "UPDATE _qmf_registration_runs "
                "SET status='executing', result_code='', "
                "execution_started_at=UTC_TIMESTAMP() "
                "WHERE id=%s AND status='prepared' AND expires_at>UTC_TIMESTAMP()",
                (run_id,),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "登记准备记录已过期，请重新预演")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        if lock_acquired:
            async with conn.cursor() as cur:
                await cur.execute("SELECT RELEASE_LOCK(%s)", (_QMF_CLAIM_LOCK,))
    return await _load_run(conn, run_id, user_id=int(user["id"]))


async def _source_row_key(conn, run: dict[str, Any]) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT row_key FROM _online_source_rows WHERE id=%s AND parser_type=%s",
            (run["source_id"], run["parser_type"]),
        )
        row = await cur.fetchone()
    if not row:
        raise QmfPreviewError(
            "source_missing", "腾讯来源行已不存在，请重新预演", 409
        )
    return str(row[0] or "")


async def _current_run_source(
    conn,
    *,
    run: dict[str, Any],
    user: dict,
) -> tuple[dict[str, Any], str]:
    row_key = await _source_row_key(conn, run)
    request_data = QmfPreviewRequest(
        parser_type=run["parser_type"],
        row_key=row_key,
        source_id=run["source_id"],
        expected_revision=run["expected_revision"],
    )
    return await _eligible_platform_task(request_data, user=user, conn=conn)


async def _append_tencent_marker(
    conn,
    *,
    run: dict[str, Any],
    user: dict,
    request_stub: Request,
    strict_source: bool,
) -> str:
    row_key = await _source_row_key(conn, run)
    detail = await _mobile_task_detail_data(
        run["parser_type"],
        row_key,
        user,
        conn,
        include_photo_requests=False,
    )
    sources = detail.get("sources") or []
    source = next(
        (item for item in sources if int(item.get("id") or 0) == run["source_id"]),
        None,
    )
    if not source or len(sources) != 1 or detail.get("task", {}).get("conflict"):
        raise QmfPreviewError(
            "tencent_source_conflict", "腾讯来源状态已变化，完成标记等待人工重试", 409
        )
    current_note = str((source.get("values") or {}).get("备注") or "")
    if TENCENT_MARKER in current_note:
        return "already_present"
    if strict_source:
        await _assert_source_unchanged(
            conn,
            parser_type=run["parser_type"],
            row_key=row_key,
            source_id=run["source_id"],
            expected_revision=run["expected_revision"],
            expected_hash=run["_expected_row_hash"],
        )
    separator = "" if not current_note else (
        "" if current_note.endswith(("；", ";", "\n")) else "；"
    )
    next_note = f"{current_note}{separator}{TENCENT_MARKER}"

    def validate_current_note(values: dict[str, Any]) -> None:
        if str(values.get("备注") or "") != current_note:
            raise HTTPException(409, "腾讯备注已变化，完成标记等待人工重试")

    await update_source_fields(
        parser_type=run["parser_type"],
        source_id=run["source_id"],
        changes={"备注": next_note},
        expected_revision=int(source["revision"]),
        request=request_stub,
        user=user,
        conn=conn,
        explicit_text_edit=True,
        allowed_columns={"备注"},
        current_values_validator=validate_current_note,
        redact_audit_values=True,
        system_managed_columns={"备注"},
    )
    return "written"


_qmf_background_tasks: set[asyncio.Task] = set()


def _background_request(audit_fields: dict[str, str]) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if audit_fields.get("user_agent"):
        headers.append((
            b"user-agent",
            audit_fields["user_agent"].encode("latin-1", errors="ignore"),
        ))
    if audit_fields.get("ip_address"):
        headers.append((
            b"x-forwarded-for",
            audit_fields["ip_address"].encode("ascii", errors="ignore"),
        ))
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/qmf-registration/background",
        "headers": headers,
        "client": (audit_fields.get("ip_address") or "127.0.0.1", 0),
    })


async def _set_run_result(
    conn,
    run_id: int,
    *,
    status: str,
    result_code: str,
    photo: dict[str, Any] | None = None,
    upstream_task_digest: str = "",
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE _qmf_registration_runs SET status=%s, result_code=%s, "
            "photo_sha256=COALESCE(NULLIF(%s,''),photo_sha256), "
            "photo_mime_type=COALESCE(NULLIF(%s,''),photo_mime_type), "
            "photo_size_bytes=CASE WHEN %s>0 THEN %s ELSE photo_size_bytes END, "
            "upstream_task_digest=COALESCE(NULLIF(%s,''),upstream_task_digest), "
            "completed_at=CASE WHEN %s='succeeded' THEN UTC_TIMESTAMP() "
            "ELSE completed_at END WHERE id=%s",
            (
                status,
                result_code[:64],
                str((photo or {}).get("sha256") or "")[:64],
                str((photo or {}).get("mime_type") or "")[:50],
                int((photo or {}).get("size_bytes") or 0),
                int((photo or {}).get("size_bytes") or 0),
                upstream_task_digest[:64],
                status,
                run_id,
            ),
        )


async def _set_marker_status(
    conn,
    run_id: int,
    status: str,
    error_code: str = "",
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE _qmf_registration_runs SET tencent_marker_status=%s, "
            "tencent_marker_error=%s WHERE id=%s",
            (status[:32], error_code[:64], run_id),
        )


async def _claim_marker_retry(conn, run_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE _qmf_registration_runs "
            "SET tencent_marker_status='writing', tencent_marker_error='' "
            "WHERE id=%s AND status='succeeded' "
            "AND tencent_marker_status IN ('pending','conflict','failed')",
            (run_id,),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "该运行不需要或不允许重试腾讯完成标记")


async def _execute_run_background(
    run_id: int,
    *,
    user: dict,
    audit_fields: dict[str, str],
) -> None:
    started_at = time.monotonic()
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    result_code = "unexpected_error"
    final_status = "failed"
    marker_status = "not_started"
    qmf_success_persisted = False
    try:
        run = await _load_run(conn, run_id, user_id=int(user["id"]))
        config = await load_qmf_config(conn)
        if not config.registration_configured:
            raise QmfPreviewError(
                "registration_not_configured",
                "全民防真实登记已由超级管理员关闭",
                503,
            )
        platform_task, current_hash = await _current_run_source(
            conn, run=run, user=user
        )
        if current_hash != run["_expected_row_hash"]:
            raise QmfPreviewError(
                "source_changed", "腾讯来源行已变化，请重新预演", 409
            )
        await _assert_source_unchanged(
            conn,
            parser_type=run["parser_type"],
            row_key=platform_task["row_key"],
            source_id=run["source_id"],
            expected_revision=run["expected_revision"],
            expected_hash=run["_expected_row_hash"],
        )

        async def step_callback(key: str, status: str, code: str) -> None:
            await _update_run_step(conn, run_id, key, status, code)

        async def before_write(_context: QmfCollectedContext) -> None:
            current_config = await load_qmf_config(conn)
            if not current_config.registration_configured:
                raise QmfPreviewError(
                    "registration_disabled_before_write",
                    "全民防真实登记已在写入前关闭",
                    503,
                )
            if not await _online_writeback_available(conn):
                raise QmfPreviewError(
                    "tencent_writeback_disabled",
                    "在线回写已暂停，已在全民防写入前停止",
                    503,
                )
            legacy_status = await _legacy_status_for_task(
                conn,
                platform_task=platform_task,
                source_id=run["source_id"],
            )
            ensure_registration_allowed(legacy_status)
            await _assert_source_unchanged(
                conn,
                parser_type=run["parser_type"],
                row_key=platform_task["row_key"],
                source_id=run["source_id"],
                expected_revision=run["expected_revision"],
                expected_hash=run["_expected_row_hash"],
            )

        result = await run_guarded_registration(
            platform_task=platform_task,
            step_callback=step_callback,
            before_write=before_write,
            config=config,
        )
        photo = result.get("photo") or {}
        result_code = "success"
        final_status = "succeeded"
        await _set_run_result(
            conn,
            run_id,
            status="succeeded",
            result_code=result_code,
            photo=photo,
            upstream_task_digest=_safe_digest(
                "qmf-task", result.get("upstream_task_id")
            ),
        )
        qmf_success_persisted = True

        try:
            await _set_marker_status(conn, run_id, "writing")
            request_stub = _background_request(audit_fields)
            marker_result = await _append_tencent_marker(
                conn,
                run=run,
                user=user,
                request_stub=request_stub,
                strict_source=True,
            )
            marker_status = "succeeded"
            await _set_marker_status(
                conn,
                run_id,
                marker_status,
                "already_present" if marker_result == "already_present" else "",
            )
        except QmfPreviewError as exc:
            marker_status = "conflict" if exc.status_code == 409 else "pending"
            await _set_marker_status(conn, run_id, marker_status, exc.code)
        except HTTPException as exc:
            marker_status = "conflict" if exc.status_code == 409 else "pending"
            await _set_marker_status(
                conn, run_id, marker_status, f"http_{exc.status_code}"
            )
        except Exception:
            marker_status = "pending"
            try:
                await _set_marker_status(
                    conn, run_id, marker_status, "unexpected_error"
                )
            except Exception:
                pass
    except QmfPreviewError as exc:
        result_code = exc.code
        await _finish_sending_step(
            conn,
            run_id,
            status="uncertain" if exc.uncertain else "failed",
            result_code=exc.code,
        )
        current_run = await _load_run(conn, run_id, user_id=int(user["id"]))
        partial_write_succeeded = any(
            item["key"] in WRITE_STEP_KEYS
            and item["status"] == "succeeded"
            for item in current_run["steps"]
        )
        final_status = "uncertain" if exc.uncertain or partial_write_succeeded else "failed"
        await _set_run_result(
            conn,
            run_id,
            status=final_status,
            result_code=result_code,
        )
    except Exception:
        if qmf_success_persisted:
            # The external registration and final review are already known to
            # have succeeded.  Marker bookkeeping must never downgrade that
            # result; a later retry will first read the current Tencent note.
            final_status = "succeeded"
            result_code = "success"
            marker_status = "pending"
            try:
                await _set_marker_status(
                    conn, run_id, marker_status, "marker_state_update_failed"
                )
            except Exception:
                pass
        else:
            run = await _load_run(conn, run_id, user_id=int(user["id"]))
            has_write_progress = any(
                item["key"] in WRITE_STEP_KEYS
                and item["status"] in {"sending", "succeeded"}
                for item in run["steps"]
            )
            final_status = "uncertain" if has_write_progress else "failed"
            await _finish_sending_step(
                conn,
                run_id,
                status="uncertain" if has_write_progress else "failed",
                result_code="unexpected_error",
            )
            await _set_run_result(
                conn,
                run_id,
                status=final_status,
                result_code="unexpected_error",
            )
    finally:
        pool.release(conn)
        await record_admin_audit(
            user,
            "qmf_registration.execute",
            target_type="qmf_registration_run",
            target_name=str(run_id),
            result="success" if final_status == "succeeded" else final_status,
            detail={
                "run_id": run_id,
                "status": final_status,
                "result_code": result_code,
                "tencent_marker_status": marker_status,
                "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            },
            **audit_fields,
        )


async def _freeze_unstarted_background_run(
    run_id: int,
    *,
    user: dict,
    audit_fields: dict[str, str],
    result_code: str = "background_start_failed",
) -> None:
    """Freeze a claimed run after its background coroutine stops unexpectedly.

    This recovery only updates local state.  It never retries or calls an
    external 全民防 endpoint.  If any write may have started, the result is
    uncertain; otherwise it is a local failure that is still permanently
    frozen from automatic re-execution.
    """
    pool = db_manager.get_pool("online_data")
    conn = None
    changed = False
    try:
        conn = await pool.acquire()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, steps_json, tencent_marker_status "
                "FROM _qmf_registration_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
            if not row:
                return
            current_status = str(row[0] or "")
            marker_status = str(row[2] or "not_started")
            if current_status == "succeeded":
                if marker_status != "writing":
                    return
                await cur.execute(
                    "UPDATE _qmf_registration_runs "
                    "SET tencent_marker_status='pending', tencent_marker_error=%s "
                    "WHERE id=%s AND status='succeeded' "
                    "AND tencent_marker_status='writing'",
                    (result_code[:64], run_id),
                )
                changed = cur.rowcount == 1
                recovered_status = "succeeded"
                recovered_marker_status = "pending"
                recovered_result_code = "success"
            elif current_status != "executing":
                return
            else:
                steps = parse_steps(row[1])
                write_steps = {
                    "upload_photo", "save_local_photo", "register_person", "complete_task",
                    "complete_task_non_jurisdiction_retry"
                }
                has_write_progress = any(
                    item["key"] in write_steps
                    and item["status"] in {"sending", "succeeded"}
                    for item in steps
                )
                recovered_status = "uncertain" if has_write_progress else "failed"
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                for item in steps:
                    if item["status"] != "sending":
                        continue
                    item["status"] = recovered_status
                    item["result_code"] = result_code[:64]
                    item["finished_at"] = now
                await cur.execute(
                    "UPDATE _qmf_registration_runs "
                    "SET status=%s, result_code=%s, steps_json=%s "
                    "WHERE id=%s AND status='executing'",
                    (
                        recovered_status,
                        result_code[:64],
                        serialize_steps(steps),
                        run_id,
                    ),
                )
                changed = cur.rowcount == 1
                recovered_marker_status = marker_status
                recovered_result_code = result_code[:64]
    except Exception:
        # Startup schema recovery will turn a surviving executing row into
        # uncertain if the database itself is currently unavailable.
        return
    finally:
        if conn is not None:
            pool.release(conn)
    if changed:
        try:
            await record_admin_audit(
                user,
                "qmf_registration.execute",
                target_type="qmf_registration_run",
                target_name=str(run_id),
                result=recovered_status,
                detail={
                    "run_id": run_id,
                    "status": recovered_status,
                    "result_code": recovered_result_code,
                    "tencent_marker_status": recovered_marker_status,
                },
                **audit_fields,
            )
        except Exception:
            # The run has already been frozen.  An audit outage must not turn
            # this local recovery into an unhandled background exception.
            pass


def _background_task_finished(
    task: asyncio.Task,
    *,
    run_id: int,
    user: dict,
    audit_fields: dict[str, str],
) -> None:
    _qmf_background_tasks.discard(task)
    result_code = "background_task_cancelled" if task.cancelled() else ""
    try:
        failed = bool(result_code) or task.exception() is not None
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        failed = True
        result_code = "background_task_cancelled"
    if not failed:
        return
    recovery = asyncio.create_task(
        _freeze_unstarted_background_run(
            run_id,
            user=dict(user),
            audit_fields=dict(audit_fields),
            result_code=result_code or "background_start_failed",
        )
    )
    _qmf_background_tasks.add(recovery)
    recovery.add_done_callback(_qmf_background_tasks.discard)


def _launch_registration_run(
    run_id: int,
    *,
    user: dict,
    audit_fields: dict[str, str],
) -> None:
    task = asyncio.create_task(
        _execute_run_background(run_id, user=dict(user), audit_fields=dict(audit_fields))
    )
    _qmf_background_tasks.add(task)
    task.add_done_callback(
        lambda completed: _background_task_finished(
            completed,
            run_id=run_id,
            user=user,
            audit_fields=audit_fields,
        )
    )


@router.post("/status")
async def get_qmf_legacy_status(
    data: QmfPreviewRequest,
    request: Request,
    user: dict = Depends(require_permission(QMF_REGISTRATION_EXECUTE)),
    conn=Depends(get_db),
):
    started_at = time.monotonic()
    audit_result = "failed"
    audit_state = "unexpected_error"
    try:
        platform_task, current_hash = await _eligible_platform_task(
            data,
            user=user,
            conn=conn,
            resolve_registration_fields=False,
        )
        await _assert_source_unchanged(
            conn,
            parser_type=data.parser_type,
            row_key=data.row_key,
            source_id=data.source_id,
            expected_revision=data.expected_revision,
            expected_hash=current_hash,
        )
        status = await _legacy_status_for_task(
            conn,
            platform_task=platform_task,
            source_id=data.source_id,
        )
        audit_state = status.state
        audit_result = "success" if status.state != "unavailable" else "failed"
        return JSONResponse(
            content=status.public_payload(),
            headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"},
        )
    except QmfPreviewError as exc:
        audit_state = exc.code
        raise HTTPException(
            exc.status_code, _safe_error_detail(exc.code, exc.message)
        ) from exc
    finally:
        await record_admin_audit(
            user,
            "qmf_registration.status.read",
            target_type="online_source_row",
            target_name=str(data.source_id),
            result=audit_result,
            detail={
                "source_id": data.source_id,
                "state": audit_state[:64],
                "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            },
            **request_audit_fields(request),
        )


@router.post("/prepare")
async def prepare_qmf_registration(
    data: QmfPreviewRequest,
    request: Request,
    user: dict = Depends(require_permission(QMF_REGISTRATION_EXECUTE)),
    conn=Depends(get_db),
):
    started_at = time.monotonic()
    try:
        runtime_config = await load_qmf_config(conn)
        if not runtime_config.registration_configured:
            raise QmfPreviewError(
                "registration_not_configured",
                "全民防真实登记尚未完成安全配置",
                503,
            )
        if not await _online_writeback_available(conn):
            raise QmfPreviewError(
                "tencent_writeback_disabled",
                "在线回写已暂停，不能创建全民防登记准备",
                503,
            )
        if qmf_operation_busy() or await _database_registration_active(conn):
            raise QmfPreviewError(
                "registration_busy", "已有一条全民防任务正在执行", 429
            )
        platform_task, current_hash = await _eligible_platform_task(
            data, user=user, conn=conn
        )
        await _assert_source_unchanged(
            conn,
            parser_type=data.parser_type,
            row_key=data.row_key,
            source_id=data.source_id,
            expected_revision=data.expected_revision,
            expected_hash=current_hash,
        )
        legacy_status = await _legacy_status_for_task(
            conn,
            platform_task=platform_task,
            source_id=data.source_id,
        )
        ensure_registration_allowed(legacy_status)
        result = await run_guarded_preview(
            platform_task=platform_task,
            config=runtime_config,
        )
        await _assert_source_unchanged(
            conn,
            parser_type=data.parser_type,
            row_key=data.row_key,
            source_id=data.source_id,
            expected_revision=data.expected_revision,
            expected_hash=current_hash,
        )
        run = await _create_prepared_run(
            conn,
            data=data,
            user=user,
            expected_hash=current_hash,
            preview=result,
        )
        result = {
            **result,
            "mode": "prepared",
            "can_submit": True,
            "run": _public_run(run),
            "planned_write_steps": [
                {"key": key, "label": label, "enabled": True}
                for key, label in steps_for_result(platform_task["result"])
            ],
            "warnings": [
                *(result.get("warnings") or []),
                (
                    "真实登记只会执行一次模型三注销反馈；提交后不能撤销。"
                    if platform_task["result"] == RESULT_LEAVE_NOT_RETURNING
                    else "真实登记会依次上传照片、保存人员资料并反馈模型三；提交后不能撤销。"
                ),
            ],
        }
        await record_admin_audit(
            user,
            "qmf_registration.prepare",
            target_type="qmf_registration_run",
            target_name=str(run["id"]),
            detail={
                "run_id": run["id"],
                "source_id": data.source_id,
                "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
                **({"photo": {
                    "mime_type": str(
                        (result.get("photo") or {}).get("mime_type") or ""
                    )[:50],
                    "size_bytes": int(
                        (result.get("photo") or {}).get("size_bytes") or 0
                    ),
                    "sha256": str(
                        (result.get("photo") or {}).get("sha256") or ""
                    )[:64],
                }} if result.get("photo") else {}),
            },
            **request_audit_fields(request),
        )
        return JSONResponse(
            content=result,
            headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"},
        )
    except QmfPreviewError as exc:
        await record_admin_audit(
            user,
            "qmf_registration.prepare",
            target_type="online_source_row",
            target_name=str(data.source_id),
            result="failed",
            detail={
                "source_id": data.source_id,
                "result_code": exc.code,
                **({"error_step": exc.step[:64]} if exc.step else {}),
                **(
                    {"upstream_http_status": int(exc.upstream_status)}
                    if exc.upstream_status is not None else {}
                ),
                **(
                    {"transport_error": exc.transport_error}
                    if exc.transport_error in _QMF_SAFE_TRANSPORT_ERRORS else {}
                ),
                "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            },
            **request_audit_fields(request),
        )
        raise HTTPException(
            exc.status_code, _safe_error_detail(exc.code, exc.message)
        ) from exc
    except HTTPException as exc:
        await record_admin_audit(
            user,
            "qmf_registration.prepare",
            target_type="online_source_row",
            target_name=str(data.source_id),
            result="failed",
            detail={
                "source_id": data.source_id,
                "result_code": f"platform_http_{exc.status_code}",
                "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            },
            **request_audit_fields(request),
        )
        raise
    except Exception as exc:
        await record_admin_audit(
            user,
            "qmf_registration.prepare",
            target_type="online_source_row",
            target_name=str(data.source_id),
            result="failed",
            detail={
                "source_id": data.source_id,
                "result_code": "unexpected_error",
                "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            },
            **request_audit_fields(request),
        )
        raise HTTPException(500, "全民防真实登记准备失败") from exc


@router.post("/runs/{run_id}/execute", status_code=202)
async def execute_qmf_registration(
    run_id: int,
    data: QmfExecuteRequest,
    request: Request,
    user: dict = Depends(require_permission(QMF_REGISTRATION_EXECUTE)),
    conn=Depends(get_db),
):
    config = await load_qmf_config(conn)
    run = await _claim_run(conn, run_id, user=user, config=config)
    try:
        _launch_registration_run(
            run_id,
            user=user,
            audit_fields=request_audit_fields(request),
        )
    except Exception as exc:
        await _set_run_result(
            conn,
            run_id,
            status="failed",
            result_code="background_start_failed",
        )
        raise HTTPException(500, "全民防登记任务启动失败，已冻结本次执行") from exc
    await asyncio.sleep(0)
    return _public_run(
        await _load_run(conn, run_id, user_id=int(user["id"]))
    )


@router.get("/runs/{run_id}")
async def get_qmf_registration_run(
    run_id: int,
    user: dict = Depends(require_permission(QMF_REGISTRATION_EXECUTE)),
    conn=Depends(get_db),
):
    return _public_run(
        await _load_run(conn, run_id, user_id=int(user["id"]))
    )


@router.post("/runs/{run_id}/retry-marker")
async def retry_qmf_tencent_marker(
    run_id: int,
    request: Request,
    user: dict = Depends(require_permission(QMF_REGISTRATION_EXECUTE)),
    conn=Depends(get_db),
):
    run = await _load_run(conn, run_id, user_id=int(user["id"]))
    if run["status"] != "succeeded" or not run["can_retry_marker"]:
        raise HTTPException(409, "该运行不需要或不允许重试腾讯完成标记")
    await _claim_marker_retry(conn, run_id)
    audit_result = "failed"
    error_code = "unexpected_error"
    try:
        await _append_tencent_marker(
            conn,
            run=run,
            user=user,
            request_stub=request,
            strict_source=False,
        )
        await _set_marker_status(conn, run_id, "succeeded")
        audit_result = "success"
        error_code = ""
    except QmfPreviewError as exc:
        marker_status = "conflict" if exc.status_code == 409 else "pending"
        await _set_marker_status(conn, run_id, marker_status, exc.code)
        error_code = exc.code
        raise HTTPException(
            exc.status_code, _safe_error_detail(exc.code, exc.message)
        ) from exc
    except HTTPException as exc:
        marker_status = "conflict" if exc.status_code == 409 else "pending"
        await _set_marker_status(conn, run_id, marker_status, f"http_{exc.status_code}")
        error_code = f"http_{exc.status_code}"
        raise
    except Exception as exc:
        await _set_marker_status(conn, run_id, "pending", "unexpected_error")
        raise HTTPException(500, "腾讯完成标记写入失败，请稍后人工重试") from exc
    finally:
        await record_admin_audit(
            user,
            "qmf_registration.tencent_marker.retry",
            target_type="qmf_registration_run",
            target_name=str(run_id),
            result=audit_result,
            detail={
                "run_id": run_id,
                "result_code": error_code,
            },
            **request_audit_fields(request),
        )
    return _public_run(
        await _load_run(conn, run_id, user_id=int(user["id"]))
    )


@router.post("/preview")
async def preview_qmf_registration(
    data: QmfPreviewRequest,
    request: Request,
    user: dict = Depends(require_permission(QMF_REGISTRATION_EXECUTE)),
    conn=Depends(get_db),
):
    started_at = time.monotonic()
    try:
        runtime_config = (
            await load_qmf_config(conn)
            if hasattr(conn, "cursor")
            else settings_config()
        )
        if not preview_configured(runtime_config):
            raise QmfPreviewError(
                "preview_not_configured",
                "全民防只读预演尚未完成安全配置",
                503,
            )
        platform_task, current_hash = await _eligible_platform_task(
            data, user=user, conn=conn
        )
        await _assert_source_unchanged(
            conn,
            parser_type=data.parser_type,
            row_key=data.row_key,
            source_id=data.source_id,
            expected_revision=data.expected_revision,
            expected_hash=current_hash,
        )
        result = await run_guarded_preview(
            platform_task=platform_task,
            config=runtime_config,
        )
        # 上游读取可能持续数秒；返回敏感资料前再次确认平台来源仍未变化。
        await _assert_source_unchanged(
            conn,
            parser_type=data.parser_type,
            row_key=data.row_key,
            source_id=data.source_id,
            expected_revision=data.expected_revision,
            expected_hash=current_hash,
        )
        await _record_preview_audit(
            request=request,
            user=user,
            source_id=data.source_id,
            result="success",
            started_at=started_at,
            photo=result.get("photo"),
        )
        return JSONResponse(
            content=result,
            headers={
                "Cache-Control": "no-store, private",
                "Pragma": "no-cache",
            },
        )
    except QmfPreviewError as exc:
        await _record_preview_audit(
            request=request,
            user=user,
            source_id=data.source_id,
            result="failed",
            started_at=started_at,
            error_code=exc.code,
            error_step=exc.step,
            upstream_status=exc.upstream_status,
            transport_error=exc.transport_error,
        )
        raise HTTPException(
            exc.status_code,
            _safe_error_detail(exc.code, exc.message),
        ) from exc
    except HTTPException:
        await _record_preview_audit(
            request=request,
            user=user,
            source_id=data.source_id,
            result="failed",
            started_at=started_at,
            error_code="platform_task_unavailable",
        )
        raise
    except Exception as exc:
        await _record_preview_audit(
            request=request,
            user=user,
            source_id=data.source_id,
            result="failed",
            started_at=started_at,
            error_code="unexpected_error",
        )
        raise HTTPException(
            500,
            _safe_error_detail(
                "unexpected_error", "全民防只读预演暂时不可用，请稍后重试"
            ),
        ) from exc
