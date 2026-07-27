"""超级管理员站内通知 API。"""

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_db
from deps import require_super_admin

router = APIRouter(prefix="/api/notifications", tags=["站内通知"])


def _iso_utc(value) -> str | None:
    return value.isoformat() + "Z" if value else None


@router.get("/unread-count")
async def get_unread_count(
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM _notifications "
            "WHERE user_id=%s AND is_read=0",
            (user["id"],),
        )
        row = await cur.fetchone()
    return {"unread_count": row[0] if row else 0}


@router.get("")
async def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM _notifications "
            "WHERE user_id=%s AND is_read=0",
            (user["id"],),
        )
        unread_count = (await cur.fetchone())[0]
        await cur.execute(
            """
            SELECT
                id, category, severity, title, content,
                related_task_id, is_read, created_at, read_at
            FROM _notifications
            WHERE user_id=%s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (user["id"], limit),
        )
        rows = await cur.fetchall()

    return {
        "unread_count": unread_count,
        "data": [
            {
                "id": row[0],
                "category": row[1],
                "severity": row[2],
                "title": row[3],
                "content": row[4],
                "related_task_id": row[5],
                "is_read": bool(row[6]),
                "created_at": _iso_utc(row[7]),
                "read_at": _iso_utc(row[8]),
            }
            for row in rows
        ],
    }


@router.post("/read-all")
async def mark_all_read(
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE _notifications
            SET is_read=1, read_at=UTC_TIMESTAMP()
            WHERE user_id=%s AND is_read=0
            """,
            (user["id"],),
        )
    return {"message": "全部通知已标记为已读"}


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: int,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE _notifications
            SET is_read=1, read_at=COALESCE(read_at, UTC_TIMESTAMP())
            WHERE id=%s AND user_id=%s
            """,
            (notification_id, user["id"]),
        )
        if cur.rowcount == 0:
            await cur.execute(
                "SELECT id FROM _notifications WHERE id=%s AND user_id=%s",
                (notification_id, user["id"]),
            )
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="通知不存在")
    return {"message": "通知已标记为已读"}
