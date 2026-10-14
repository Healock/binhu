"""平台功能模块注册表与单位访问判定。

模块归属是现有权限之外的一层门禁：模块必须已启用，账号还必须满足
模块归属和 required_permissions 两个条件。代码目录只负责首次安装默认项；
运行时以数据库注册表为准，版本表保留归属变更快照。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from services.permissions import has_permission

COMMON_UNIT = "common"
COMMUNITY_POLICE_UNIT = "community_police"

MODULE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "module_id": "common", "name": "公共功能", "owner_unit": COMMON_UNIT,
        "is_public": True, "required_permissions": [], "route": "/", "mobile_visible": True,
    },
    {
        "module_id": "community_police", "name": "社区警务", "owner_unit": COMMUNITY_POLICE_UNIT,
        "is_public": False, "required_permissions": [], "route": "/query", "mobile_visible": True,
    },
    {
        "module_id": "workflow", "name": "工单中心", "owner_unit": COMMUNITY_POLICE_UNIT,
        "is_public": False, "required_permissions": ["workflow.ticket.view"],
        "route": "/workflow", "mobile_visible": True,
    },
    {
        "module_id": "platform_admin", "name": "平台管理", "owner_unit": "internal",
        "is_public": False, "required_permissions": [], "route": "/users", "mobile_visible": True,
    },
)

def _admin_access(user: dict[str, Any]) -> bool:
    if str(user.get("role") or "") in {"admin", "super_admin"}:
        return True
    return any(
        str(group.get("code") or "") in {"admin", "super_admin"}
        for group in user.get("permission_groups") or []
    )


def _has_owner_unit(user: dict[str, Any], owner_unit: str) -> bool:
    if owner_unit == COMMON_UNIT:
        return True
    departments = user.get("departments") or []
    if owner_unit == COMMUNITY_POLICE_UNIT:
        return any(str(item.get("type") or "") == "community" for item in departments if isinstance(item, dict))
    return any(str(item.get("type") or "") == owner_unit for item in departments if isinstance(item, dict))


def _required_permissions(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    return [str(item) for item in value or [] if str(item).strip()]


def can_access_definition(user: dict[str, Any], definition: dict[str, Any]) -> bool:
    if not definition.get("enabled", True):
        return False
    if (
        not _admin_access(user)
        and not definition.get("is_public")
        and not _has_owner_unit(user, str(definition.get("owner_unit") or ""))
    ):
        return False
    return all(
        has_permission(user, permission)
        for permission in _required_permissions(definition.get("required_permissions"))
    )


def available_module_ids(user: dict[str, Any], definitions: Iterable[dict[str, Any]] | None = None) -> list[str]:
    source = definitions if definitions is not None else MODULE_DEFINITIONS
    return sorted(str(item["module_id"]) for item in source if can_access_definition(user, item))


async def load_module_definitions(cur) -> list[dict[str, Any]]:
    await cur.execute(
        "SELECT module_id, name, owner_unit, is_public, required_permissions, "
        "route, mobile_visible, enabled, version FROM _function_modules "
        "ORDER BY module_id"
    )
    rows = [row for row in await cur.fetchall() if len(row) >= 9]
    return [
        {
            "module_id": str(row[0]), "name": str(row[1]), "owner_unit": str(row[2] or ""),
            "is_public": bool(row[3]), "required_permissions": _required_permissions(row[4]),
            "route": str(row[5] or ""), "mobile_visible": bool(row[6]),
            "enabled": bool(row[7]), "version": int(row[8] or 1),
        }
        for row in rows
    ]


async def available_module_ids_from_cursor(user: dict[str, Any], cur) -> list[str]:
    return available_module_ids(user, await load_module_definitions(cur))


async def user_can_access_module(user: dict[str, Any], module_id: str, cur=None) -> bool:
    definitions = await load_module_definitions(cur) if cur is not None else list(MODULE_DEFINITIONS)
    definition = next((item for item in definitions if item.get("module_id") == module_id), None)
    return bool(definition and can_access_definition(user, definition))


async def ensure_function_module_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _function_modules (
            module_id VARCHAR(60) NOT NULL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            owner_unit VARCHAR(60) NOT NULL DEFAULT '',
            is_public TINYINT(1) NOT NULL DEFAULT 0,
            required_permissions JSON NOT NULL,
            route VARCHAR(190) NOT NULL DEFAULT '',
            mobile_visible TINYINT(1) NOT NULL DEFAULT 1,
            enabled TINYINT(1) NOT NULL DEFAULT 1,
            version INT UNSIGNED NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_function_module_owner (owner_unit, enabled),
            INDEX idx_function_module_public (is_public, enabled)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _function_module_versions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            module_id VARCHAR(60) NOT NULL,
            version INT UNSIGNED NOT NULL,
            snapshot JSON NOT NULL,
            changed_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_function_module_version (module_id, version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    for definition in MODULE_DEFINITIONS:
        snapshot = json.dumps(definition, ensure_ascii=False, separators=(",", ":"))
        await cur.execute(
            "INSERT IGNORE INTO _function_modules "
            "(module_id, name, owner_unit, is_public, required_permissions, route, mobile_visible, enabled, version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,1,1)",
            (
                definition["module_id"], definition["name"], definition["owner_unit"],
                int(definition["is_public"]),
                json.dumps(definition["required_permissions"], ensure_ascii=False),
                definition["route"], int(definition["mobile_visible"]),
            ),
        )
        await cur.execute(
            "INSERT IGNORE INTO _function_module_versions (module_id, version, snapshot) VALUES (%s, 1, %s)",
            (definition["module_id"], snapshot),
        )
