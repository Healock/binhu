"""管理员悬浮任务队列的安全只读聚合。

这里只汇总任务类型、阶段、数量和时间，不返回任务 payload、业务正文、
人员信息、外部响应或底层错误内容。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from database import db_manager
from services.local_source import local_data_source_enabled


RECENT_WINDOW_MINUTES = 30
MAX_ITEMS_PER_SOURCE = 20

ACTIVE_STATES = {"queued", "running", "retrying"}

EXTERNAL_KIND_LABELS = {
    "code_summary_fetch": "平安码与管家码数据获取",
    "visit_source_preview": "走访来源数据获取",
    "photo_sheet_preview": "调照片名单预览",
    "photo_sheet_sync": "调照片名单同步",
    "qmf_source": "全民防未核查任务同步",
    "residence_full_scan": "居住证登记状态全量查询",
}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_task_state(status: object) -> str:
    value = str(status or "").strip().lower()
    if value in {"queued", "pending", "prepared", "pending_confirmation"}:
        return "queued"
    if value in {"running", "executing", "processing", "writing", "sending"}:
        return "running"
    if value in {"retry", "retryable", "reconciling"}:
        return "retrying"
    if value in {"success", "completed", "confirmed", "done"}:
        return "success"
    if value in {"partial", "warning", "uncertain", "reconciliation_required", "conflict"}:
        return "warning"
    if value in {"paused", "pending_review"}:
        return "paused"
    if value in {"cancelled", "canceled", "interrupted", "superseded"}:
        return "cancelled"
    if value in {"failed", "error"}:
        return "failed"
    return "warning"


def _progress(current: int | None, total: int | None) -> int | None:
    if total is None or total <= 0 or current is None:
        return None
    return max(0, min(100, round(current * 100 / total)))


def _item(
    *,
    source: str,
    source_id: int | str,
    category: str,
    title: str,
    status: object,
    phase: str = "",
    current: int | None = None,
    total: int | None = None,
    message: str = "",
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    updated_at: datetime | None = None,
    detail_count: int = 0,
    attention_count: int = 0,
    retry_kind: str | None = None,
) -> dict[str, Any]:
    state = normalize_task_state(status)
    return {
        "id": f"{source}:{source_id}",
        "source": source,
        "category": category,
        "title": title,
        "state": state,
        "phase": str(phase or ""),
        "current": int(current) if current is not None else None,
        "total": int(total) if total is not None else None,
        "progress": _progress(current, total),
        "message": str(message or "")[:200],
        "active": state in ACTIVE_STATES,
        "created_at": _iso(created_at),
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "updated_at": _iso(updated_at or finished_at or started_at or created_at),
        "detail_count": max(0, int(detail_count or 0)),
        "attention_count": max(0, int(attention_count or 0)),
        "retry_kind": retry_kind,
    }


async def _rows(pool_name: str, sql: str) -> list[tuple]:
    try:
        pool = db_manager.get_pool(pool_name)
    except ValueError:
        # 兼容尚未启用业务分库的旧部署：这些表在迁移前均位于
        # OnlineData。若旧库本身也没有对应表，外层仍会把该来源标记为
        # unavailable，而不会影响其他任务来源。
        pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return list(await cur.fetchall())


async def _query_rows(
    pool_name: str,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[tuple]:
    try:
        pool = db_manager.get_pool(pool_name)
    except ValueError:
        pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())


ONLINE_WRITEBACK_DIAGNOSIS = {
    "source_missing": (
        "腾讯来源行已经不存在，平台无法继续定位原记录。",
        "请先执行一次正常同步，再回到任务详情核对来源状态。",
    ),
    "source_relocated": (
        "腾讯来源行的位置或身份已经变化，平台为防止写错行而停止写回。",
        "请先同步腾讯在线表，再在任务详情中核对并重新保存。",
    ),
    "remote_changed": (
        "腾讯端内容已被其他人修改，与平台保存时的版本不一致。",
        "请在任务详情查看最新内容，人工决定采用腾讯内容还是重新修改。",
    ),
    "row_key_conflict": (
        "任务来源标识不再唯一，继续自动写回可能写到错误记录。",
        "请先同步并核对重复来源，不要直接重试。",
    ),
}


PHOTO_WRITEBACK_DIAGNOSIS = {
    "source_disabled": (
        "调照片腾讯名单写回当前未启用。",
        "请先在工单流程设置中确认写回开关和目标表配置。",
    ),
    "source_missing": (
        "原腾讯名单行已经不存在或无法安全定位。",
        "请先同步调照片名单，确认原工单仍有唯一来源后再重试。",
    ),
    "source_relocated": (
        "原腾讯名单行位置已经变化，系统已停止自动写回。",
        "请先同步名单重新定位，再执行安全重试。",
    ),
    "source_revised": (
        "腾讯来源字段已修订，平台已将重复工单合并到原任务。",
        "无需重试；请在原任务详情核对合并后的来源状态。",
    ),
    "quota_exhausted": (
        "腾讯接口当前额度不足，自动重试已经暂停。",
        "额度恢复后可手动重新加入写回队列。",
    ),
    "request_failed": (
        "腾讯接口连续请求失败，达到自动重试上限。",
        "确认网络和腾讯接口恢复后再手动重试。",
    ),
}


def _diagnosis(
    mapping: dict[str, tuple[str, str]],
    error_code: object,
    *,
    fallback_diagnosis: str,
    fallback_action: str,
) -> tuple[str, str]:
    code = str(error_code or "").strip().lower()
    return mapping.get(code, (fallback_diagnosis, fallback_action))


async def get_admin_task_queue_details(
    source: str,
    *,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """返回固定白名单队列的脱敏问题明细。"""
    offset = (page - 1) * page_size
    if source == "online_writeback_queue":
        if local_data_source_enabled():
            return {
                "source": source,
                "page": page,
                "page_size": page_size,
                "total": 0,
                "data": [],
                "message": "本地数据源已启用，无腾讯字段回写队列",
            }
        count_rows, rows = await asyncio.gather(
            _query_rows(
                "online_data",
                "SELECT COUNT(*) FROM _online_local_changes "
                "WHERE status IN ('retry','conflict')",
            ),
            _query_rows(
                "online_data",
                "SELECT id,parser_type,row_key,field_name,status,attempt_count,"
                "error_code,updated_at FROM _online_local_changes "
                "WHERE status IN ('retry','conflict') "
                "ORDER BY FIELD(status,'conflict','retry'),updated_at DESC,id DESC "
                "LIMIT %s OFFSET %s",
                (page_size, offset),
            ),
        )
        data = []
        for row in rows:
            diagnosis, action = _diagnosis(
                ONLINE_WRITEBACK_DIAGNOSIS,
                row[6],
                fallback_diagnosis=(
                    "字段写回遇到异常或冲突，平台已保留本地修改并停止危险重放。"
                ),
                fallback_action="请先同步腾讯在线表，再回到对应任务详情核对。",
            )
            row_key = str(row[2] or "")
            data.append({
                "id": int(row[0]),
                "state": str(row[4]),
                "reference": f"{row[1]} · {row_key[:8]}…" if row_key else str(row[1]),
                "action": f"字段：{row[3]}",
                "attempt_count": int(row[5] or 0),
                "error_code": str(row[6] or "unknown"),
                "diagnosis": diagnosis,
                "recommended_action": action,
                "can_retry": False,
                "retry_kind": None,
                "updated_at": _iso(row[7]),
            })
    elif source == "photo_writeback_queue":
        if local_data_source_enabled():
            return {
                "source": source,
                "page": page,
                "page_size": page_size,
                "total": 0,
                "data": [],
                "message": "本地数据源已启用，无腾讯照片名单回写队列",
            }
        count_rows, rows = await asyncio.gather(
            _query_rows(
                "workflow",
                "SELECT COUNT(*) FROM photo_sheet_outbox "
                "WHERE status IN ('retry','paused')",
            ),
            _query_rows(
                "workflow",
                "SELECT id,work_order_id,action,status,attempt_count,error_code,updated_at "
                "FROM photo_sheet_outbox WHERE status IN ('retry','paused') "
                "ORDER BY FIELD(status,'paused','retry'),updated_at DESC,id DESC "
                "LIMIT %s OFFSET %s",
                (page_size, offset),
            ),
        )
        action_labels = {
            "complete": "写回完成标记",
            "claim": "写回领取状态",
            "update": "更新腾讯名单",
        }
        data = []
        for row in rows:
            diagnosis, action = _diagnosis(
                PHOTO_WRITEBACK_DIAGNOSIS,
                row[5],
                fallback_diagnosis="调照片名单写回连续失败，系统已停止自动重试。",
                fallback_action="确认外部系统和名单配置恢复后，可执行一次安全重试。",
            )
            data.append({
                "id": int(row[0]),
                "state": str(row[3]),
                "reference": f"工单 #{int(row[1])}",
                "action": action_labels.get(str(row[2]), "调照片名单写回"),
                "attempt_count": int(row[4] or 0),
                "error_code": str(row[5] or "unknown"),
                "diagnosis": diagnosis,
                "recommended_action": action,
                "can_retry": True,
                "retry_kind": "photo_outbox",
                "updated_at": _iso(row[6]),
            })
    else:
        raise ValueError("unsupported task queue source")

    total = int(count_rows[0][0] or 0) if count_rows else 0
    return {
        "source": source,
        "page": page,
        "page_size": page_size,
        "total": total,
        "data": data,
    }


async def _external_jobs() -> list[dict[str, Any]]:
    rows = await _rows("online_data", f"""
        SELECT id,kind,status,phase,current_count,total_count,progress_message,
               created_at,started_at,finished_at,updated_at
        FROM _external_acquisition_runs
        WHERE status IN ('queued','running')
           OR updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {RECENT_WINDOW_MINUTES} MINUTE)
        ORDER BY (status IN ('queued','running')) DESC, updated_at DESC
        LIMIT {MAX_ITEMS_PER_SOURCE}
    """)
    return [
        _item(
            source="external_acquisition",
            source_id=row[0],
            category=(
                "全民防同步" if str(row[1]) == "qmf_source"
                else "居住证查询" if str(row[1]) == "residence_full_scan"
                else "数据获取"
            ),
            title=EXTERNAL_KIND_LABELS.get(str(row[1]), "外部数据获取"),
            status=row[2],
            phase=row[3],
            current=row[4],
            total=row[5],
            message=row[6],
            created_at=row[7],
            started_at=row[8],
            finished_at=row[9],
            updated_at=row[10],
        )
        for row in rows
    ]


async def _dispatch_publish_jobs() -> list[dict[str, Any]]:
    rows = await _rows("dispatch", f"""
        SELECT id,status,phase,total_count,processed_count,created_at,
               started_at,finished_at,updated_at
        FROM _police_dispatch_publish_runs
        WHERE status IN ('pending','running')
           OR updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {RECENT_WINDOW_MINUTES} MINUTE)
        ORDER BY (status IN ('pending','running')) DESC,updated_at DESC
        LIMIT {MAX_ITEMS_PER_SOURCE}
    """)
    return [
        _item(
            source="dispatch_publish",
            source_id=row[0],
            category="本地任务发布" if local_data_source_enabled() else "发布与回写",
            title="本地任务发布" if local_data_source_enabled() else "下发任务发布",
            status=row[1],
            phase=row[2],
            current=row[4],
            total=row[3],
            created_at=row[5],
            started_at=row[6],
            finished_at=row[7],
            updated_at=row[8],
        )
        for row in rows
    ]


async def _archive_jobs() -> list[dict[str, Any]]:
    rows = await _rows("online_data", f"""
        SELECT id,status,phase,total_count,
               (success_count+conflict_count+error_count),
               created_at,started_at,finished_at,updated_at
        FROM _fullchain_archive_exports
        WHERE status IN ('queued','running')
           OR updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {RECENT_WINDOW_MINUTES} MINUTE)
        ORDER BY (status IN ('queued','running')) DESC,updated_at DESC
        LIMIT {MAX_ITEMS_PER_SOURCE}
    """)
    return [
        _item(
            source="fullchain_archive",
            source_id=row[0],
            category="归档处理",
            title="全链条导出归档",
            status=row[1],
            phase=row[2],
            current=row[4],
            total=row[3],
            created_at=row[5],
            started_at=row[6],
            finished_at=row[7],
            updated_at=row[8],
        )
        for row in rows
    ]


async def _registry_certificate_jobs() -> list[dict[str, Any]]:
    rows = await _rows("registry", f"""
        SELECT id,status,phase,current_page,fetched_count,created_at,
               started_at,finished_at,updated_at
        FROM registry_certificate_source_runs
        WHERE status IN ('pending','running')
           OR updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {RECENT_WINDOW_MINUTES} MINUTE)
        ORDER BY (status IN ('pending','running')) DESC,updated_at DESC
        LIMIT {MAX_ITEMS_PER_SOURCE}
    """)
    return [
        _item(
            source="registry_certificate",
            source_id=row[0],
            category="数据获取",
            title="房东责任告知书读取",
            status=row[1],
            phase=row[2],
            current=row[4],
            total=None,
            message=f"已读取 {int(row[3] or 0)} 页",
            created_at=row[5],
            started_at=row[6],
            finished_at=row[7],
            updated_at=row[8],
        )
        for row in rows
    ]


async def _qmf_jobs() -> list[dict[str, Any]]:
    scan_rows = await _rows("online_data", f"""
        SELECT id,status,total_count,processed_count,created_at,
               started_at,finished_at,updated_at
        FROM _qmf_status_scan_runs
        WHERE status IN ('queued','running')
           OR updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {RECENT_WINDOW_MINUTES} MINUTE)
        ORDER BY (status IN ('queued','running')) DESC,updated_at DESC
        LIMIT {MAX_ITEMS_PER_SOURCE}
    """)
    return [
        _item(
            source="qmf_status_scan",
            source_id=row[0],
            category="数据核对",
            title="全民防反馈状态扫描",
            status=row[1],
            phase="scan",
            current=row[3],
            total=row[2],
            created_at=row[4],
            started_at=row[5],
            finished_at=row[6],
            updated_at=row[7],
        )
        for row in scan_rows
    ]


async def _visit_import_jobs() -> list[dict[str, Any]]:
    rows = await _rows("visit", f"""
        SELECT id,status,import_type,total_rows,
               (inserted_rows+updated_rows+unchanged_rows+ignored_rows+error_count),
               created_at,started_at,finished_at
        FROM _visit_import_batches
        WHERE status='running'
           OR finished_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {RECENT_WINDOW_MINUTES} MINUTE)
        ORDER BY (status='running') DESC,COALESCE(finished_at,started_at,created_at) DESC
        LIMIT {MAX_ITEMS_PER_SOURCE}
    """)
    return [
        _item(
            source="visit_import",
            source_id=row[0],
            category="数据导入",
            title="走访明细导入" if row[2] == "detail" else "走访星级数据导入",
            status=row[1],
            phase="import",
            current=row[4],
            total=row[3],
            created_at=row[5],
            started_at=row[6],
            finished_at=row[7],
        )
        for row in rows
    ]


async def _backup_jobs() -> list[dict[str, Any]]:
    rows = await _rows("platform", f"""
        SELECT id,status,trigger_source,created_at,started_at,finished_at
        FROM _backup_jobs
        WHERE status IN ('pending','running')
           OR finished_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {RECENT_WINDOW_MINUTES} MINUTE)
        ORDER BY (status IN ('pending','running')) DESC,
                 COALESCE(finished_at,started_at,created_at) DESC
        LIMIT {MAX_ITEMS_PER_SOURCE}
    """)
    return [
        _item(
            source="backup",
            source_id=row[0],
            category="系统任务",
            title="数据库备份",
            status=row[1],
            phase="backup",
            message="自动备份" if row[2] == "scheduled" else "手动备份",
            created_at=row[3],
            started_at=row[4],
            finished_at=row[5],
        )
        for row in rows
    ]


TaskLoader = Callable[[], Awaitable[list[dict[str, Any]]]]


async def build_admin_task_queue() -> dict[str, Any]:
    loaders: list[tuple[str, TaskLoader]] = [
        ("外部数据获取", _external_jobs),
        ("下发任务发布", _dispatch_publish_jobs),
        ("全链条归档", _archive_jobs),
        ("责任告知书读取", _registry_certificate_jobs),
        ("全民防任务", _qmf_jobs),
        ("走访导入", _visit_import_jobs),
        ("数据库备份", _backup_jobs),
    ]
    results = await asyncio.gather(
        *(loader() for _, loader in loaders),
        return_exceptions=True,
    )
    items: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for (label, _), result in zip(loaders, results, strict=True):
        if isinstance(result, Exception):
            unavailable.append(label)
            continue
        items.extend(result)

    active = [item for item in items if item["active"]]
    recent = sorted(
        (item for item in items if not item["active"]),
        key=lambda item: item["updated_at"] or "",
        reverse=True,
    )
    active.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    visible_items = (active + recent)[:60]
    return {
        "server_time": _iso(datetime.now(timezone.utc)),
        "refresh_after_seconds": 10,
        "active_count": len(active),
        "queued_count": sum(item["state"] == "queued" for item in active),
        "running_count": sum(item["state"] == "running" for item in active),
        "attention_count": sum(
            item["state"] in {"failed", "warning", "paused", "retrying"}
            for item in visible_items
        ),
        "items": visible_items,
        "unavailable_sources": unavailable,
    }
