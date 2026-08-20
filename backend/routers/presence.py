"""在线状态心跳与在线用户目录。"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from database import db_manager
from config import settings
from deps import get_current_user, require_permission
from services.permissions import PRESENCE_DETAIL_VIEW


ONLINE_WINDOW_SECONDS = 90

router = APIRouter(prefix="/api/presence", tags=["在线状态"])


class PresenceHeartbeatRequest(BaseModel):
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() + "Z"


async def _presence_summary(cur) -> tuple[int, datetime]:
    await cur.execute("SELECT UTC_TIMESTAMP()")
    row = await cur.fetchone()
    server_time = row[0] if row else datetime.utcnow()
    await cur.execute(
        """
        SELECT COUNT(DISTINCT presence.user_id)
        FROM _user_presence_clients AS presence
        JOIN _sessions AS session ON session.session_id=presence.session_id
        JOIN _users AS user ON user.id=presence.user_id
        LEFT JOIN _system_config AS idle_config
          ON idle_config.config_key='session_idle_minutes'
        WHERE presence.last_seen_at >= UTC_TIMESTAMP() - INTERVAL %s SECOND
          AND session.user_id=presence.user_id
          AND session.expires_at>UTC_TIMESTAMP()
          AND TIMESTAMPDIFF(SECOND, session.last_activity_at, UTC_TIMESTAMP())
              < COALESCE(NULLIF(CAST(idle_config.config_value AS UNSIGNED), 0), 30) * 60
        """,
        (ONLINE_WINDOW_SECONDS,),
    )
    count_row = await cur.fetchone()
    return int(count_row[0] or 0) if count_row else 0, server_time


@router.post("/heartbeat")
async def presence_heartbeat(
    payload: PresenceHeartbeatRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not session_id:
        raise HTTPException(status_code=401, detail="登录会话已失效")
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO _user_presence_clients
                    (client_id, user_id, session_id, last_seen_at)
                VALUES (%s, %s, %s, UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE
                    user_id=VALUES(user_id),
                    session_id=VALUES(session_id),
                    last_seen_at=UTC_TIMESTAMP()
                """,
                (payload.client_id, user["id"], session_id),
            )
            count, server_time = await _presence_summary(cur)
    finally:
        pool.release(conn)
    return {
        "online_count": count,
        "server_time": _iso_utc(server_time),
        "online_window_seconds": ONLINE_WINDOW_SECONDS,
    }


@router.get("/users")
async def presence_users(
    user: dict = Depends(require_permission(PRESENCE_DETAIL_VIEW)),
):
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user.id,
                       COALESCE(NULLIF(user.display_name, ''), NULLIF(member.name, ''), '平台用户'),
                       user.avatar_storage_key,
                       member.position,
                       department.name,
                       community.name,
                       MAX(presence.last_seen_at)
                FROM _user_presence_clients AS presence
                JOIN _sessions AS session ON session.session_id=presence.session_id
                JOIN _users AS user ON user.id=presence.user_id
                LEFT JOIN _system_config AS idle_config
                  ON idle_config.config_key='session_idle_minutes'
                LEFT JOIN _grid_members AS member ON member.id=user.member_id
                LEFT JOIN _departments AS department ON department.id=member.department_id
                LEFT JOIN _communities AS community ON community.id=department.community_id
                WHERE presence.last_seen_at >= UTC_TIMESTAMP() - INTERVAL %s SECOND
                  AND session.user_id=presence.user_id
                  AND session.expires_at>UTC_TIMESTAMP()
                  AND TIMESTAMPDIFF(SECOND, session.last_activity_at, UTC_TIMESTAMP())
                      < COALESCE(NULLIF(CAST(idle_config.config_value AS UNSIGNED), 0), 30) * 60
                GROUP BY user.id, user.display_name, member.name,
                         user.avatar_storage_key, member.name, member.position,
                         department.name, community.name
                ORDER BY member.position, community.name, department.name,
                         COALESCE(NULLIF(user.display_name, ''), member.name, user.username)
                """,
                (ONLINE_WINDOW_SECONDS,),
            )
            rows = await cur.fetchall()
    finally:
        pool.release(conn)

    users = []
    for row in rows:
        department = row[5] or row[4] or None
        users.append({
            "id": int(row[0]),
            "display_name": str(row[1]),
            "avatar_url": (
                f"/api/auth/avatar/{int(row[0])}"
                if row[2] else None
            ),
            "position": str(row[3] or ""),
            "department": str(department) if department else None,
            "last_seen_at": _iso_utc(row[6]),
        })
    return {
        "online_count": len(users),
        "online_window_seconds": ONLINE_WINDOW_SECONDS,
        "users": users,
    }
