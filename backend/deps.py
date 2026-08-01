"""认证、会话与权限依赖。"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request, status

from config import settings
from database import db_manager
from services.mobile_navigation import (
    normalize_mobile_dock_config,
    normalize_mobile_navigation_mode,
)
from services.permissions import (
    COMMUNITY_VIEW,
    NOTIFICATION_VIEW,
    PERSONNEL_BASIC_VIEW,
    PREFERENCES_MANAGE,
    SYNC_TRIGGER,
    has_permission,
    legacy_permissions,
    parse_permissions,
)
from services.theme_preferences import normalize_theme_mode


def _auth_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
    )


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


async def get_current_user(request: Request) -> dict:
    """验证唯一会话、空闲时间并返回实时权限上下文。"""
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not session_id:
        raise _auth_error("not_authenticated", "未登录")

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user.id, user.username, user.role,
                       user.table_display_mode, user.report_column_mode,
                       user.mobile_navigation_mode, user.mobile_dock_config,
                       user.theme_mode, user.member_id,
                       user.password_is_temporary, user.active_session_id,
                       permission_group.id, permission_group.code,
                       permission_group.name, permission_group.permissions,
                       permission_group.data_scope,
                       member.name, member.position,
                       department.id, department.name,
                       department.department_type, community.name,
                       session.created_at, session.last_activity_at,
                       session.expires_at, UTC_TIMESTAMP(),
                       user.display_name
                FROM _sessions AS session
                JOIN _users AS user ON user.id=session.user_id
                LEFT JOIN _permission_groups AS permission_group
                  ON permission_group.id=user.permission_group_id
                LEFT JOIN _grid_members AS member ON member.id=user.member_id
                LEFT JOIN _departments AS department
                  ON department.id=member.department_id
                LEFT JOIN _communities AS community
                  ON community.id=department.community_id
                WHERE session.session_id=%s
                """,
                (session_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise _auth_error("session_expired", "登录会话已失效")

            active_session_id = row[10]
            if active_session_id and active_session_id != session_id:
                raise _auth_error(
                    "session_replaced",
                    "账号已在另一台设备登录",
                )

            created_at: datetime = row[22]
            last_activity_at: datetime = row[23] or created_at
            expires_at: datetime = row[24]
            server_time: datetime = row[25]
            if expires_at <= server_time:
                raise _auth_error("session_expired", "登录有效期已结束")

            await cur.execute(
                "SELECT config_key, config_value FROM _system_config "
                "WHERE config_key IN "
                "('session_idle_minutes', 'permission_enforcement_enabled')"
            )
            config = {str(item[0]): str(item[1]) for item in await cur.fetchall()}
            try:
                idle_minutes = int(config.get("session_idle_minutes", "30"))
            except (TypeError, ValueError):
                idle_minutes = 30
            idle_minutes = min(1440, max(5, idle_minutes))
            if (server_time - last_activity_at).total_seconds() >= idle_minutes * 60:
                raise _auth_error(
                    "session_idle_timeout",
                    "长时间未操作，请重新登录",
                )

            if request.headers.get("X-User-Activity") == "1":
                await cur.execute(
                    "UPDATE _sessions SET last_activity_at=UTC_TIMESTAMP() "
                    "WHERE session_id=%s",
                    (session_id,),
                )
                last_activity_at = server_time

            if row[11] is not None:
                permissions = parse_permissions(row[14])
                data_scope = str(row[15] or "own_department")
                group_code = str(row[12])
                group_name = str(row[13])
            elif not _as_bool(config.get("permission_enforcement_enabled")):
                permissions, data_scope, group_code = legacy_permissions(
                    str(row[2])
                )
                group_name = {
                    "super_admin": "超级管理员",
                    "admin": "管理员",
                    "legacy_viewer": "原账号兼容权限",
                }.get(group_code, group_code)
            else:
                permissions = sorted({
                    PERSONNEL_BASIC_VIEW,
                    COMMUNITY_VIEW,
                    NOTIFICATION_VIEW,
                    PREFERENCES_MANAGE,
                })
                data_scope = "own_department"
                group_code = "unassigned"
                group_name = "待关联人员"

            department = None
            if row[18] is not None:
                department = {
                    "id": row[18],
                    "name": row[19],
                    "type": row[20],
                    "community_name": row[21],
                }
            member = None
            if row[8] is not None:
                member = {
                    "id": row[8],
                    "name": row[16],
                    "position": row[17],
                }

            return {
                "id": row[0],
                "username": row[1],
                "display_name": str(row[26] or row[16] or row[1]),
                "role": row[2],
                "table_display_mode": row[3] or "table",
                "report_column_mode": row[4] or "three",
                "mobile_navigation_mode": normalize_mobile_navigation_mode(
                    row[5]
                ),
                "mobile_dock_config": normalize_mobile_dock_config(
                    row[6], str(row[2]), permissions
                ),
                "theme_mode": normalize_theme_mode(row[7]),
                "member": member,
                "department": department,
                "permission_group": {
                    "id": row[11],
                    "code": group_code,
                    "name": group_name,
                },
                "permissions": permissions,
                "data_scope": data_scope,
                "password_is_temporary": bool(row[9]),
                "session_policy": {
                    "idle_timeout_minutes": idle_minutes,
                    "warning_seconds": 120,
                    "last_activity_at": last_activity_at.isoformat() + "Z",
                    "absolute_expires_at": expires_at.isoformat() + "Z",
                    "server_time": server_time.isoformat() + "Z",
                },
            }
    finally:
        pool.release(conn)


def require_permission(permission: str) -> Callable:
    async def dependency(user: dict = Depends(get_current_user)) -> dict:
        if not has_permission(user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前权限组不能执行此操作",
            )
        return user

    dependency.__name__ = f"require_{permission.replace('.', '_')}"
    return dependency


async def require_super_admin(
    user: dict = Depends(get_current_user),
) -> dict:
    group_code = (user.get("permission_group") or {}).get("code")
    legacy_super_admin = group_code is None and user.get("role") == "super_admin"
    if group_code != "super_admin" and not legacy_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要超级管理员权限",
        )
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """兼容旧路由：内勤业务组及以上可以进入日常业务操作。"""
    legacy_admin = not user.get("permissions") and user.get("role") in {
        "admin",
        "super_admin",
    }
    if not has_permission(user, SYNC_TRIGGER) and not legacy_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前权限组不能执行此业务操作",
        )
    return user


def create_session(user_id: int) -> str:
    del user_id
    return secrets.token_urlsafe(32)


def get_session_cookie_config():
    return {
        "key": settings.SESSION_COOKIE_NAME,
        "httponly": True,
        "secure": settings.SESSION_COOKIE_SECURE,
        "samesite": settings.SESSION_COOKIE_SAMESITE,
        "path": "/",
        "max_age": settings.SESSION_EXPIRE_HOURS * 3600,
    }
