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
from services.member_departments import get_member_departments
from services.maintenance import enforce_maintenance, is_super_admin_user


FEATURE_PERMISSION_GATES = {
    "registry": {
        "registry.property.view",
        "registry.property.manage",
        "registry.watch.view",
        "registry.watch.manage",
        "registry.import.manage",
    },
    "workflow": {
        "workflow.ticket.create",
        "workflow.ticket.view",
        "workflow.ticket.handle",
        "workflow.ticket.manage",
        "workflow.config.manage",
        "workflow.attachment.view",
    },
}


def _apply_feature_permission_gates(
    permissions: list[str],
    permission_scopes: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """在功能迁移完成前，彻底从权限上下文移除未启用域。"""
    disabled: set[str] = set()
    if not settings.REGISTRY_FEATURE_ENABLED:
        disabled.update(FEATURE_PERMISSION_GATES["registry"])
    if not settings.WORKFLOW_FEATURE_ENABLED:
        disabled.update(FEATURE_PERMISSION_GATES["workflow"])
    result = [item for item in permissions if item not in disabled]
    scopes = {key: value for key, value in permission_scopes.items() if key in result}
    return result, scopes


def _auth_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
    )


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


async def _load_effective_groups(
    cur,
    *,
    user_id: int,
    member_id: int | None,
    assignment_mode: str,
    primary_group_id: int | None,
) -> list[dict[str, Any]]:
    if assignment_mode == "inherited" and member_id is not None:
        await cur.execute(
            """
            SELECT DISTINCT permission_group.id, permission_group.code,
                   permission_group.name, permission_group.permissions,
                   permission_group.data_scope, permission_group.sort_order
            FROM _grid_members AS member
            JOIN _position_permission_group_links AS link
              ON link.position=member.position
            JOIN _permission_groups AS permission_group
              ON permission_group.id=link.permission_group_id
            WHERE member.id=%s
            ORDER BY permission_group.sort_order, permission_group.id
            """,
            (member_id,),
        )
    else:
        await cur.execute(
            """
            SELECT DISTINCT permission_group.id, permission_group.code,
                   permission_group.name, permission_group.permissions,
                   permission_group.data_scope, permission_group.sort_order
            FROM _user_permission_group_links AS link
            JOIN _permission_groups AS permission_group
              ON permission_group.id=link.permission_group_id
            WHERE link.user_id=%s
            ORDER BY permission_group.sort_order, permission_group.id
            """,
            (user_id,),
        )
    rows = await cur.fetchall()
    if not rows and primary_group_id is not None:
        await cur.execute(
            """
            SELECT id, code, name, permissions, data_scope, sort_order
            FROM _permission_groups WHERE id=%s
            """,
            (primary_group_id,),
        )
        fallback = await cur.fetchone()
        rows = [fallback] if fallback else []
    return [
        {
            "id": int(row[0]),
            "code": str(row[1]),
            "name": str(row[2]),
            "permissions": parse_permissions(row[3]),
            "data_scope": str(row[4] or "own_department"),
        }
        for row in rows
    ]


async def _load_current_user(
    request: Request,
    *,
    check_maintenance: bool,
) -> dict:
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
                       user.group_assignment_mode,
                       user.password_is_temporary, user.active_session_id,
                       user.permission_group_id,
                       member.name, member.position,
                       department.id, department.name,
                       department.department_type, community.name,
                       session.created_at, session.last_activity_at,
                       session.expires_at, UTC_TIMESTAMP(),
                       user.display_name
                FROM _sessions AS session
                JOIN _users AS user ON user.id=session.user_id
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

            active_session_id = row[11]
            if active_session_id and active_session_id != session_id:
                raise _auth_error(
                    "session_replaced",
                    "账号已在另一台设备登录",
                )

            created_at: datetime = row[19]
            last_activity_at: datetime = row[20] or created_at
            expires_at: datetime = row[21]
            server_time: datetime = row[22]
            if expires_at <= server_time:
                raise _auth_error("session_expired", "登录有效期已结束")

            await cur.execute(
                "SELECT config_key, config_value FROM _system_config "
                "WHERE config_key IN "
                "('session_idle_minutes', 'permission_enforcement_enabled', "
                "'maintenance_enabled', 'maintenance_start_at', "
                "'maintenance_end_at', 'maintenance_message', 'timezone')"
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

            groups = await _load_effective_groups(
                cur,
                user_id=int(row[0]),
                member_id=int(row[8]) if row[8] is not None else None,
                assignment_mode=str(row[9] or "inherited"),
                primary_group_id=(
                    int(row[12]) if row[12] is not None else None
                ),
            )
            if groups:
                permissions_set: set[str] = set()
                permission_scopes: dict[str, str] = {}
                for group in groups:
                    for permission in group["permissions"]:
                        permissions_set.add(permission)
                        if (
                            permission_scopes.get(permission) == "all"
                            or group["data_scope"] == "all"
                        ):
                            permission_scopes[permission] = "all"
                        else:
                            permission_scopes[permission] = "own_department"
                permissions = sorted(permissions_set)
                data_scope = (
                    "all"
                    if permission_scopes
                    and all(value == "all" for value in permission_scopes.values())
                    else "own_department"
                )
            elif not _as_bool(config.get("permission_enforcement_enabled")):
                permissions, data_scope, group_code = legacy_permissions(
                    str(row[2])
                )
                group_name = {
                    "super_admin": "超级管理员",
                    "admin": "管理员",
                    "legacy_viewer": "原账号兼容权限",
                }.get(group_code, group_code)
                groups = [{
                    "id": None,
                    "code": group_code,
                    "name": group_name,
                    "permissions": permissions,
                    "data_scope": data_scope,
                }]
                permission_scopes = {
                    permission: data_scope for permission in permissions
                }

            permissions, permission_scopes = _apply_feature_permission_gates(
                permissions,
                permission_scopes,
            )
            if permissions:
                data_scope = (
                    "all"
                    if all(value == "all" for value in permission_scopes.values())
                    else "own_department"
                )
            else:
                permissions = sorted({
                    PERSONNEL_BASIC_VIEW,
                    COMMUNITY_VIEW,
                    NOTIFICATION_VIEW,
                    PREFERENCES_MANAGE,
                })
                data_scope = "own_department"
                groups = [{
                    "id": None,
                    "code": "unassigned",
                    "name": "待关联人员",
                    "permissions": permissions,
                    "data_scope": data_scope,
                }]
                permission_scopes = {
                    permission: data_scope for permission in permissions
                }

            # 维护模式由后端强制执行；超级管理员仍可登录和处理维护配置。
            # 不能只依赖前端隐藏菜单，否则已有会话仍可继续访问业务接口。
            maintenance_user = {
                "role": row[2],
                "permission_group": {"code": groups[0]["code"]} if groups else None,
                "permission_groups": groups,
            }
            if check_maintenance and not is_super_admin_user(maintenance_user):
                enforce_maintenance(config, maintenance_user, now=server_time)

            primary_group = groups[0]

            fallback_department = None
            if row[15] is not None:
                fallback_department = {
                    "id": row[15],
                    "name": row[16],
                    "type": row[17],
                    "community_name": row[18],
                }
            departments = []
            if row[8] is not None:
                departments = (
                    await get_member_departments(cur, [int(row[8])])
                ).get(int(row[8]), [])
            if not departments and fallback_department:
                departments = [fallback_department]
            department = departments[0] if departments else None
            member = None
            if row[8] is not None:
                member = {
                    "id": row[8],
                    "name": row[13],
                    "position": row[14],
                }

            return {
                "id": row[0],
                "username": row[1],
                "display_name": str(row[23] or row[13] or row[1]),
                "role": row[2],
                "table_display_mode": row[3] or "table",
                "report_column_mode": row[4] or "three",
                "mobile_navigation_mode": normalize_mobile_navigation_mode(
                    row[5]
                ),
                "mobile_dock_config": normalize_mobile_dock_config(
                    row[6],
                    str(row[2]),
                    permissions,
                    [group["code"] for group in groups],
                    row[14],
                ),
                "theme_mode": normalize_theme_mode(row[7]),
                "member": member,
                "department": department,
                "departments": departments,
                "community_names": [
                    item["community_name"] for item in departments
                    if item.get("community_name")
                ],
                "permission_group": {
                    "id": primary_group["id"],
                    "code": primary_group["code"],
                    "name": primary_group["name"],
                },
                "permission_groups": [
                    {
                        "id": group["id"],
                        "code": group["code"],
                        "name": group["name"],
                    }
                    for group in groups
                ],
                "permissions": permissions,
                "data_scope": data_scope,
                "permission_scopes": permission_scopes,
                "password_is_temporary": bool(row[10]),
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


async def get_current_user(request: Request) -> dict:
    return await _load_current_user(request, check_maintenance=True)


async def get_bootstrap_user(request: Request) -> dict | None:
    """bootstrap 允许匿名访问，并在维护期间保留当前账号能力信息。"""
    if not request.cookies.get(settings.SESSION_COOKIE_NAME):
        return None
    return await _load_current_user(request, check_maintenance=False)


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
    group_codes = {
        group.get("code")
        for group in user.get("permission_groups") or []
    }
    group_code = (user.get("permission_group") or {}).get("code")
    legacy_super_admin = not group_codes and user.get("role") == "super_admin"
    if (
        "super_admin" not in group_codes
        and group_code != "super_admin"
        and not legacy_super_admin
    ):
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
