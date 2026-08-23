"""管理员悬浮任务队列的安全只读聚合。

这里只汇总任务类型、阶段、数量和时间，不返回任务 payload、业务正文、
人员信息、外部响应或底层错误内容。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from database import db_manager


RECENT_WINDOW_MINUTES = 30
MAX_ITEMS_PER_SOURCE = 20

ACTIVE_STATES = {"queued", "running", "retrying"}

EXTERNAL_KIND_LABELS = {
    "code_summary_fetch": "平安码与管家码数据获取",
    "visit_source_preview": "走访来源数据获取",
    "photo_sheet_preview": "调照片名单预览",
    "photo_sheet_sync": "调照片名单同步",
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
            category="数据获取",
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


async def _sync_jobs() -> list[dict[str, Any]]:
    rows = await _rows("online_data", f"""
        SELECT id,status,trigger_source,phase,total_steps,completed_steps,
               total_rows,processed_rows,started_at,finished_at
        FROM _sync_log
        WHERE status IN ('pending','running')
           OR finished_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {RECENT_WINDOW_MINUTES} MINUTE)
        ORDER BY (status IN ('pending','running')) DESC,
                 COALESCE(finished_at,started_at) DESC,id DESC
        LIMIT {MAX_ITEMS_PER_SOURCE}
    """)
    items = []
    for row in rows:
        total = int(row[4] or 0) or int(row[6] or 0) or None
        current = int(row[5] or 0) if row[4] else int(row[7] or 0)
        items.append(_item(
            source="online_sync",
            source_id=row[0],
            category="数据同步",
            title="腾讯在线表同步",
            status=row[1],
            phase=row[3],
            current=current,
            total=total,
            message="自动同步" if row[2] == "scheduled" else "手动同步",
            started_at=row[8],
            finished_at=row[9],
        ))
    return items


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
            category="发布与回写",
            title="下发任务发布",
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
    registration_rows, scan_rows = await asyncio.gather(
        _rows("platform", f"""
            SELECT id,status,tencent_marker_status,prepared_at,
                   execution_started_at,completed_at,updated_at
            FROM _qmf_registration_runs
            WHERE status='executing'
               OR tencent_marker_status IN ('pending','writing')
               OR (
                    status NOT IN ('prepared','pending_confirmation')
                    AND updated_at >= DATE_SUB(
                        UTC_TIMESTAMP(),
                        INTERVAL {RECENT_WINDOW_MINUTES} MINUTE
                    )
               )
            ORDER BY (status='executing' OR tencent_marker_status IN ('pending','writing')) DESC,
                     updated_at DESC
            LIMIT {MAX_ITEMS_PER_SOURCE}
        """),
        _rows("online_data", f"""
            SELECT id,status,total_count,processed_count,created_at,
                   started_at,finished_at,updated_at
            FROM _qmf_status_scan_runs
            WHERE status IN ('queued','running')
               OR updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {RECENT_WINDOW_MINUTES} MINUTE)
            ORDER BY (status IN ('queued','running')) DESC,updated_at DESC
            LIMIT {MAX_ITEMS_PER_SOURCE}
        """),
    )
    items = []
    for row in registration_rows:
        marker_status = str(row[2] or "")
        status = marker_status if marker_status in {"pending", "writing"} else row[1]
        items.append(_item(
            source="qmf_registration",
            source_id=row[0],
            category="全民防登记",
            title="全民防单条登记",
            status=status,
            phase="tencent_marker" if marker_status in {"pending", "writing"} else "registration",
            current=1 if normalize_task_state(status) == "success" else 0,
            total=1,
            created_at=row[3],
            started_at=row[4],
            finished_at=row[5],
            updated_at=row[6],
        ))
    items.extend(
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
    )
    return items


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


async def _writeback_queues() -> list[dict[str, Any]]:
    online_rows, photo_rows = await asyncio.gather(
        _rows("online_data", """
            SELECT status,COUNT(*),MIN(created_at),MAX(updated_at)
            FROM _online_local_changes
            WHERE status IN ('pending','processing','retry','conflict')
            GROUP BY status
        """),
        _rows("workflow", """
            SELECT status,COUNT(*),MIN(created_at),MAX(updated_at)
            FROM photo_sheet_outbox
            WHERE status IN ('pending','retry','paused')
            GROUP BY status
        """),
    )
    items = []
    if online_rows:
        counts = {str(row[0]): int(row[1] or 0) for row in online_rows}
        total = sum(counts.values())
        status = "processing" if counts.get("processing") else (
            "retry" if counts.get("retry") else (
                "conflict" if counts.get("conflict") else "pending"
            )
        )
        items.append(_item(
            source="online_writeback_queue",
            source_id="current",
            category="发布与回写",
            title="任务字段回写队列",
            status=status,
            phase="writeback_queue",
            current=counts.get("processing", 0),
            total=total,
            message=(
                f"待写回 {counts.get('pending', 0)} · 重试 {counts.get('retry', 0)}"
                f" · 冲突 {counts.get('conflict', 0)}"
            ),
            created_at=min((row[2] for row in online_rows if row[2]), default=None),
            updated_at=max((row[3] for row in online_rows if row[3]), default=None),
        ))
    if photo_rows:
        counts = {str(row[0]): int(row[1] or 0) for row in photo_rows}
        total = sum(counts.values())
        status = "retry" if counts.get("retry") else (
            "paused" if counts.get("paused") else "pending"
        )
        items.append(_item(
            source="photo_writeback_queue",
            source_id="current",
            category="发布与回写",
            title="调照片名单回写队列",
            status=status,
            phase="photo_outbox",
            current=0,
            total=total,
            message=(
                f"待处理 {counts.get('pending', 0)} · 重试 {counts.get('retry', 0)}"
                f" · 已暂停 {counts.get('paused', 0)}"
            ),
            created_at=min((row[2] for row in photo_rows if row[2]), default=None),
            updated_at=max((row[3] for row in photo_rows if row[3]), default=None),
        ))
    return items


TaskLoader = Callable[[], Awaitable[list[dict[str, Any]]]]


async def build_admin_task_queue() -> dict[str, Any]:
    loaders: list[tuple[str, TaskLoader]] = [
        ("外部数据获取", _external_jobs),
        ("在线表同步", _sync_jobs),
        ("下发任务发布", _dispatch_publish_jobs),
        ("全链条归档", _archive_jobs),
        ("责任告知书读取", _registry_certificate_jobs),
        ("全民防任务", _qmf_jobs),
        ("走访导入", _visit_import_jobs),
        ("数据库备份", _backup_jobs),
        ("回写队列", _writeback_queues),
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
