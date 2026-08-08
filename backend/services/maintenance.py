"""维护模式状态解析与访问控制。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import HTTPException, status


MAINTENANCE_CONFIG_KEYS = (
    "maintenance_enabled",
    "maintenance_start_at",
    "maintenance_end_at",
    "maintenance_message",
    "timezone",
)


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_utc_datetime(value: Any) -> datetime | None:
    """将配置中的 ISO 时间转换为带 UTC 时区的 datetime。"""
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def is_super_admin_user(user: Mapping[str, Any] | None) -> bool:
    if not user:
        return False
    if str(user.get("role") or "") == "super_admin":
        return True
    groups = user.get("permission_groups") or []
    if isinstance(groups, (list, tuple, set)):
        return any(
            isinstance(group, Mapping) and str(group.get("code") or "") == "super_admin"
            for group in groups
        )
    group = user.get("permission_group")
    return isinstance(group, Mapping) and str(group.get("code") or "") == "super_admin"


def maintenance_status(
    config: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    enabled = _as_bool(config.get("maintenance_enabled"))
    start_at = parse_utc_datetime(config.get("maintenance_start_at"))
    end_at = parse_utc_datetime(config.get("maintenance_end_at"))
    active = enabled and (start_at is None or current >= start_at) and (
        end_at is None or current < end_at
    )
    return {
        "enabled": enabled,
        "active": active,
        "scheduled": bool(enabled and start_at and current < start_at),
        "start_at": utc_iso(start_at),
        "end_at": utc_iso(end_at),
        "message": str(config.get("maintenance_message") or "平台正在维护中，请稍后再试").strip(),
        "server_time": utc_iso(current),
        "timezone": str(config.get("timezone") or "Asia/Shanghai"),
    }


def validate_maintenance_config(config: Mapping[str, Any]) -> dict[str, str]:
    """规范化维护配置；返回可直接写入 _system_config 的字符串值。"""
    enabled = _as_bool(config.get("maintenance_enabled"))
    try:
        start_at = parse_utc_datetime(config.get("maintenance_start_at"))
        end_at = parse_utc_datetime(config.get("maintenance_end_at"))
    except (TypeError, ValueError) as exc:
        raise ValueError("维护开始和结束时间必须是有效的 ISO 时间") from exc
    if start_at and end_at and end_at <= start_at:
        raise ValueError("维护结束时间必须晚于开始时间")
    message = str(config.get("maintenance_message") or "").strip()
    if len(message) > 500:
        raise ValueError("维护说明不能超过 500 个字符")
    return {
        "maintenance_enabled": "1" if enabled else "0",
        "maintenance_start_at": utc_iso(start_at) or "",
        "maintenance_end_at": utc_iso(end_at) or "",
        "maintenance_message": message,
    }


async def load_maintenance_config(cur) -> dict[str, str]:
    placeholders = ", ".join("%s" for _ in MAINTENANCE_CONFIG_KEYS)
    await cur.execute(
        "SELECT config_key, config_value FROM _system_config "
        f"WHERE config_key IN ({placeholders})",
        MAINTENANCE_CONFIG_KEYS,
    )
    return {str(row[0]): str(row[1] or "") for row in await cur.fetchall()}


async def is_database_user_super_admin(cur, user_id: int, role: str | None = None) -> bool:
    """登录尚未生成用户上下文时，检查账号或继承权限组是否为超管。"""
    if str(role or "") == "super_admin":
        return True
    await cur.execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM _users AS user
            JOIN _permission_groups AS group_by_primary
              ON group_by_primary.id=user.permission_group_id
            WHERE user.id=%s AND group_by_primary.code='super_admin'
        )
        OR EXISTS(
            SELECT 1
            FROM _user_permission_group_links AS user_link
            JOIN _permission_groups AS group_by_user
              ON group_by_user.id=user_link.permission_group_id
            WHERE user_link.user_id=%s AND group_by_user.code='super_admin'
        )
        OR EXISTS(
            SELECT 1
            FROM _users AS user
            JOIN _grid_members AS member ON member.id=user.member_id
            JOIN _position_permission_group_links AS position_link
              ON position_link.position=member.position
            JOIN _permission_groups AS group_by_position
              ON group_by_position.id=position_link.permission_group_id
            WHERE user.id=%s AND group_by_position.code='super_admin'
        )
        """,
        (user_id, user_id, user_id),
    )
    row = await cur.fetchone()
    return bool(row and row[0])


def enforce_maintenance(
    config: Mapping[str, Any],
    user: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> None:
    current = maintenance_status(config, now=now)
    if current["active"] and not is_super_admin_user(user):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "maintenance_mode",
                "message": current["message"],
                "maintenance": current,
            },
            headers={"Retry-After": "300"},
        )
