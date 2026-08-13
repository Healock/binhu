"""面向全部登录用户的公告与个人通知 API。"""

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import get_db
from config import settings
from deps import require_permission
from services.permissions import ANNOUNCEMENT_MANAGE, NOTIFICATION_VIEW
from services.audit import record_admin_audit, request_audit_fields

router = APIRouter(prefix="/api/notifications", tags=["消息中心"])
require_notification_view = require_permission(NOTIFICATION_VIEW)
require_announcement_manage = require_permission(ANNOUNCEMENT_MANAGE)


async def _workflow_task_paths(cur, ticket_ids: list[int]) -> dict[int, str]:
    if not ticket_ids or not settings.WORKFLOW_FEATURE_ENABLED:
        return {}
    workflow_schema = settings.MYSQL_WORKFLOW_DB.replace("`", "")
    placeholders = ",".join(["%s"] * len(ticket_ids))
    await cur.execute(
        f"SELECT detail.work_order_id, detail.source_parser_type, detail.source_row_key "
        f"FROM `{workflow_schema}`.photo_request_details detail "
        f"JOIN `{workflow_schema}`.work_orders order_row ON order_row.id=detail.work_order_id "
        f"WHERE detail.work_order_id IN ({placeholders}) "
        "AND detail.external_origin='platform_task' "
        "AND detail.source_parser_type<>'' AND detail.source_row_key<>'' "
        "AND order_row.type_code='photo_request' "
        "AND order_row.status IN ('approved','completed')",
        tuple(ticket_ids),
    )
    return {
        int(ticket_id): (
            f"/tasks/{quote(str(parser_type), safe='')}/{quote(str(row_key), safe='')}"
        )
        for ticket_id, parser_type, row_key in await cur.fetchall()
    }


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
    user: dict = Depends(require_notification_view),
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
    user: dict = Depends(require_notification_view),
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
        completed_photo_ticket_ids = {
            int(row[5]) for row in personal_rows
            if row[5] is not None and str(row[1]).startswith((
                "workflow_photo_done_",
                "workflow_complete_",
                "workflow_approve_",
                "workflow_external_batch_",
            ))
        }
        task_paths = await _workflow_task_paths(
            cur,
            sorted(completed_photo_ticket_ids),
        )
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
            "action_path": task_paths.get(int(row[5])) if row[5] is not None else None,
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
            "action_path": None,
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
    user: dict = Depends(require_notification_view),
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
    user: dict = Depends(require_announcement_manage),
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
    user: dict = Depends(require_notification_view),
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
    user: dict = Depends(require_announcement_manage),
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
    user: dict = Depends(require_notification_view),
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
