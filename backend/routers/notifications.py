"""面向全部登录用户的公告与个人通知 API。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import get_db
from deps import get_current_user, require_super_admin
from services.audit import record_admin_audit, request_audit_fields

router = APIRouter(prefix="/api/notifications", tags=["消息中心"])


class AnnouncementCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2000)
    severity: Literal["info", "warning"] = "info"


def _iso_utc(value) -> str | None:
    return value.isoformat() + "Z" if value else None


async def _unread_counts(cur, user_id: int) -> tuple[int, int]:
    await cur.execute(
        "SELECT COUNT(*) FROM _notifications "
        "WHERE user_id=%s AND is_read=0",
        (user_id,),
    )
    personal_row = await cur.fetchone()
    await cur.execute(
        """
        SELECT COUNT(*)
        FROM _announcements a
        LEFT JOIN _announcement_reads r
          ON r.announcement_id=a.id AND r.user_id=%s
        WHERE a.is_active=1
          AND a.published_at <= UTC_TIMESTAMP()
          AND (a.expires_at IS NULL OR a.expires_at > UTC_TIMESTAMP())
          AND r.user_id IS NULL
        """,
        (user_id,),
    )
    announcement_row = await cur.fetchone()
    return (
        int(personal_row[0] or 0) if personal_row else 0,
        int(announcement_row[0] or 0) if announcement_row else 0,
    )


@router.get("/unread-count")
async def get_unread_count(
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        personal, announcements = await _unread_counts(cur, user["id"])
    return {
        "unread_count": personal + announcements,
        "personal_unread_count": personal,
        "announcement_unread_count": announcements,
    }


@router.get("")
async def list_notifications(
    limit: int = Query(30, ge=1, le=100),
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        personal_unread, announcement_unread = await _unread_counts(
            cur,
            user["id"],
        )
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
        personal_rows = await cur.fetchall()
        await cur.execute(
            """
            SELECT
                a.id, a.severity, a.title, a.content,
                IF(r.user_id IS NULL, 0, 1) AS is_read,
                a.published_at, r.read_at
            FROM _announcements a
            LEFT JOIN _announcement_reads r
              ON r.announcement_id=a.id AND r.user_id=%s
            WHERE a.is_active=1
              AND a.published_at <= UTC_TIMESTAMP()
              AND (a.expires_at IS NULL OR a.expires_at > UTC_TIMESTAMP())
            ORDER BY a.published_at DESC, a.id DESC
            LIMIT %s
            """,
            (user["id"], limit),
        )
        announcement_rows = await cur.fetchall()

    personal_items = [
        {
            "id": row[0],
            "source": "personal",
            "category": row[1],
            "severity": row[2],
            "title": row[3],
            "content": row[4],
            "related_task_id": row[5],
            "is_read": bool(row[6]),
            "created_at": _iso_utc(row[7]),
            "read_at": _iso_utc(row[8]),
        }
        for row in personal_rows
    ]
    announcement_items = [
        {
            "id": row[0],
            "source": "announcement",
            "category": "announcement",
            "severity": row[1],
            "title": row[2],
            "content": row[3],
            "related_task_id": None,
            "is_read": bool(row[4]),
            "created_at": _iso_utc(row[5]),
            "read_at": _iso_utc(row[6]),
        }
        for row in announcement_rows
    ]
    items = personal_items + announcement_items
    items.sort(
        key=lambda item: (item["created_at"] or "", int(item["id"])),
        reverse=True,
    )
    return {
        "unread_count": personal_unread + announcement_unread,
        "personal_unread_count": personal_unread,
        "announcement_unread_count": announcement_unread,
        "data": items[:limit],
    }


@router.post("/read-all")
async def mark_all_read(
    user: dict = Depends(get_current_user),
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
        await cur.execute(
            """
            INSERT IGNORE INTO _announcement_reads (
                announcement_id, user_id, read_at
            )
            SELECT a.id, %s, UTC_TIMESTAMP()
            FROM _announcements a
            WHERE a.is_active=1
              AND a.published_at <= UTC_TIMESTAMP()
              AND (a.expires_at IS NULL OR a.expires_at > UTC_TIMESTAMP())
            """,
            (user["id"],),
        )
    return {"message": "全部消息已标记为已读"}


@router.post("/announcements")
async def create_announcement(
    data: AnnouncementCreate,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    title = data.title.strip()
    content = data.content.strip()
    if not title or not content:
        raise HTTPException(status_code=422, detail="公告标题和内容不能为空")
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO _announcements (
                severity, title, content, created_by, published_at
            ) VALUES (%s, %s, %s, %s, UTC_TIMESTAMP())
            """,
            (data.severity, title, content, user["id"]),
        )
        announcement_id = cur.lastrowid
    await record_admin_audit(
        user,
        "announcement.create",
        target_type="announcement",
        target_name=str(announcement_id),
        detail={"title": title, "severity": data.severity},
        **request_audit_fields(request),
    )
    return {"message": "公告已发布", "id": announcement_id}


@router.post("/announcements/{announcement_id}/read")
async def mark_announcement_read(
    announcement_id: int,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT IGNORE INTO _announcement_reads (
                announcement_id, user_id, read_at
            )
            SELECT id, %s, UTC_TIMESTAMP()
            FROM _announcements
            WHERE id=%s AND is_active=1
              AND published_at <= UTC_TIMESTAMP()
              AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
            """,
            (user["id"], announcement_id),
        )
        if cur.rowcount == 0:
            await cur.execute(
                """
                SELECT id FROM _announcements
                WHERE id=%s AND is_active=1
                  AND published_at <= UTC_TIMESTAMP()
                  AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
                """,
                (announcement_id,),
            )
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="公告不存在")
    return {"message": "公告已标记为已读"}


@router.delete("/announcements/{announcement_id}")
async def delete_announcement(
    announcement_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE _announcements
            SET is_active=0
            WHERE id=%s AND is_active=1
            """,
            (announcement_id,),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="公告不存在")
    await record_admin_audit(
        user,
        "announcement.delete",
        target_type="announcement",
        target_name=str(announcement_id),
        **request_audit_fields(request),
    )
    return {"message": "公告已删除"}


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: int,
    user: dict = Depends(get_current_user),
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
                raise HTTPException(status_code=404, detail="个人提示不存在")
    return {"message": "个人提示已标记为已读"}
