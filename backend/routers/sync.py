"""数据同步 API"""

import asyncio
from fastapi import APIRouter, Depends
from database import get_db, db_manager
from schemas.sync import SyncTriggerResponse, SyncStatusResponse
from services.sync_engine import SyncEngine

router = APIRouter(prefix="/api/sync", tags=["数据同步"])


@router.post("/trigger", response_model=SyncTriggerResponse)
async def trigger_sync(conn=Depends(get_db)):
    """触发全量同步"""
    # 检查是否有正在运行的任务
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM _sync_log WHERE status = 'running' LIMIT 1"
        )
        if await cur.fetchone():
            return SyncTriggerResponse(
                task_id=0,
                status="conflict",
                message="已有同步任务正在运行，请等待完成",
            )

        # 创建新任务
        await cur.execute("INSERT INTO _sync_log (status) VALUES ('pending')")
        task_id = cur.lastrowid

    # 后台执行同步
    engine = SyncEngine(db_manager.get_pool("online_data"))
    asyncio.create_task(_run_sync(engine, task_id))

    return SyncTriggerResponse(
        task_id=task_id,
        status="pending",
        message="同步任务已创建，正在后台执行",
    )


async def _run_sync(engine: SyncEngine, task_id: int):
    """后台执行同步"""
    try:
        await engine.run_full_sync(task_id)
    except Exception:
        pass  # 错误已在引擎内部处理


@router.get("/status", response_model=SyncStatusResponse)
async def sync_status(conn=Depends(get_db)):
    """获取最近一次同步状态"""
    async with conn.cursor() as cur:
        await cur.execute(
            """SELECT id, status, total_rows, processed_rows, error_message,
                      started_at, finished_at
               FROM _sync_log ORDER BY id DESC LIMIT 1"""
        )
        row = await cur.fetchone()
    if not row:
        return SyncStatusResponse(task_id=0, status="no_data", error_message="暂无同步记录")

    return SyncStatusResponse(
        task_id=row[0],
        status=row[1],
        total_rows=row[2] or 0,
        processed_rows=row[3] or 0,
        error_message=row[4],
        started_at=row[5].isoformat() + "Z" if row[5] else None,
        finished_at=row[6].isoformat() + "Z" if row[6] else None,
    )


@router.get("/history")
async def sync_history(page: int = 1, page_size: int = 20, conn=Depends(get_db)):
    """同步历史记录"""
    offset = (page - 1) * page_size
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM _sync_log")
        row = await cur.fetchone()
        total = row[0] if row else 0

        await cur.execute(
            """SELECT id, status, total_rows, processed_rows, error_message,
                      started_at, finished_at
               FROM _sync_log ORDER BY id DESC LIMIT %s OFFSET %s""",
            (page_size, offset),
        )
        rows = await cur.fetchall()

    return {
        "data": [
            {
                "task_id": r[0],
                "status": r[1],
                "total_rows": r[2] or 0,
                "processed_rows": r[3] or 0,
                "error_message": r[4],
                "started_at": r[5].isoformat() + "Z" if r[5] else None,
                "finished_at": r[6].isoformat() + "Z" if r[6] else None,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
