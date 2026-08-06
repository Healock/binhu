"""账号级手机导航配置的校验与兼容处理。"""

from __future__ import annotations

import json
from typing import Any, Iterable


DEFAULT_MOBILE_NAVIGATION_MODE = "dock"
MAX_DOCK_GROUPS = 4

GROUP_ITEMS: dict[str, tuple[str, ...]] = {
    "workspace": (
        "dashboard",
        "online_summary",
        "online_query",
        "flow_tasks",
        "visit_summary",
        "data_upload",
        "work_log",
    ),
    "resources": (
        "grid_members",
        "communities",
        "police_addresses",
        "users",
        "permission_groups",
    ),
    "system": (
        "settings",
        "operations",
    ),
}

SUPER_ADMIN_ITEMS = {"users", "permission_groups", "operations"}
ADMIN_ITEMS = {"data_upload", "work_log", "police_addresses", "flow_tasks"}

ITEM_PERMISSIONS: dict[str, str] = {
    "online_summary": "online.summary.view",
    "online_query": "online.raw.view",
    "flow_tasks": "online.raw.view",
    "visit_summary": "visit.summary.view",
    "data_upload": "visit.import",
    "work_log": "worklog.manage",
    "grid_members": "personnel.basic.view",
    "communities": "community.view",
    "police_addresses": "police.address.manage",
    "users": "user.manage",
    "permission_groups": "permission.manage",
    "operations": "ops.manage",
}
ITEM_PERMISSION_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "data_upload": ("visit.import", "police.dispatch.manage"),
}


def normalize_mobile_navigation_mode(value: Any) -> str:
    return (
        str(value)
        if value in {"sidebar", "dock"}
        else DEFAULT_MOBILE_NAVIGATION_MODE
    )


def _item_is_accessible(
    item_id: str,
    role: str,
    permissions: Iterable[str] | None = None,
    permission_group_codes: Iterable[str] | None = None,
) -> bool:
    if item_id == "flow_tasks":
        admin_codes = {"admin", "super_admin"}
        group_codes = {
            str(code).strip() for code in permission_group_codes or []
        }
        if role not in admin_codes and not (group_codes & admin_codes):
            return False
    if permissions is not None:
        alternatives = ITEM_PERMISSION_ALTERNATIVES.get(item_id)
        if alternatives:
            return any(permission in permissions for permission in alternatives)
        required = ITEM_PERMISSIONS.get(item_id)
        return required is None or required in permissions
    if item_id in SUPER_ADMIN_ITEMS:
        return role == "super_admin"
    if item_id in ADMIN_ITEMS:
        return role in {"admin", "super_admin"}
    return True


def default_mobile_dock_config(
    role: str,
    permissions: Iterable[str] | None = None,
    permission_group_codes: Iterable[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """返回当前角色默认可见的全部分类和页面。"""
    return {
        "groups": [
            {
                "id": group_id,
                "items": [
                    item_id
                    for item_id in item_ids
                    if _item_is_accessible(
                        item_id, role, permissions, permission_group_codes
                    )
                ],
            }
            for group_id, item_ids in GROUP_ITEMS.items()
        ],
    }


def _load_config(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def normalize_mobile_dock_config(
    value: Any,
    role: str,
    permissions: Iterable[str] | None = None,
    permission_group_codes: Iterable[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """读取配置时过滤未知、重复和无权限入口。

    角色变化可能让原配置暂时失效；如果已经没有任何可用分类，就恢复该角色默认值。
    """
    parsed = _load_config(value)
    raw_groups = parsed.get("groups") if parsed else None
    if not isinstance(raw_groups, list):
        return default_mobile_dock_config(
            role, permissions, permission_group_codes
        )

    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for raw_group in raw_groups[:MAX_DOCK_GROUPS]:
        if not isinstance(raw_group, dict):
            continue
        group_id = str(raw_group.get("id") or "")
        if group_id not in GROUP_ITEMS or group_id in seen_groups:
            continue

        raw_items = raw_group.get("items")
        if not isinstance(raw_items, list):
            continue
        allowed = set(GROUP_ITEMS[group_id])
        seen_items: set[str] = set()
        items: list[str] = []
        for raw_item in raw_items:
            item_id = str(raw_item or "")
            if (
                item_id not in allowed
                or item_id in seen_items
                or not _item_is_accessible(
                    item_id, role, permissions, permission_group_codes
                )
            ):
                continue
            seen_items.add(item_id)
            items.append(item_id)
        if not items:
            continue
        seen_groups.add(group_id)
        groups.append({"id": group_id, "items": items})

    if not groups:
        groups = default_mobile_dock_config(
            role, permissions, permission_group_codes
        )["groups"]
    workspace = next(
        (group for group in groups if group["id"] == "workspace"), None
    )
    if workspace is None:
        workspace = {"id": "workspace", "items": ["dashboard"]}
        groups.insert(0, workspace)
    else:
        workspace["items"] = [
            "dashboard",
            *[item for item in workspace["items"] if item != "dashboard"],
        ]
        groups = [workspace, *[group for group in groups if group is not workspace]]
    return {"groups": groups[:MAX_DOCK_GROUPS]}


def validate_mobile_dock_config(
    value: Any,
    role: str,
    permissions: Iterable[str] | None = None,
    permission_group_codes: Iterable[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """保存配置前做严格校验，避免静默接受错误或越权入口。"""
    parsed = _load_config(value)
    raw_groups = parsed.get("groups") if parsed else None
    if not isinstance(raw_groups, list):
        raise ValueError("Dock 配置格式不正确")
    if not 1 <= len(raw_groups) <= MAX_DOCK_GROUPS:
        raise ValueError("Dock 必须保留 1 至 4 个分类")

    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            raise ValueError("Dock 分类格式不正确")
        group_id = str(raw_group.get("id") or "")
        if group_id not in GROUP_ITEMS:
            raise ValueError(f"未知的 Dock 分类：{group_id or '空值'}")
        if group_id in seen_groups:
            raise ValueError("Dock 分类不能重复")

        raw_items = raw_group.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("每个 Dock 分类至少保留一个页面")
        allowed = set(GROUP_ITEMS[group_id])
        seen_items: set[str] = set()
        items: list[str] = []
        for raw_item in raw_items:
            item_id = str(raw_item or "")
            if item_id not in allowed:
                raise ValueError(f"页面不属于当前分类：{item_id or '空值'}")
            if item_id in seen_items:
                raise ValueError("同一分类中的页面不能重复")
            if not _item_is_accessible(
                item_id, role, permissions, permission_group_codes
            ):
                raise ValueError("Dock 配置包含当前账号无权访问的页面")
            seen_items.add(item_id)
            items.append(item_id)

        seen_groups.add(group_id)
        groups.append({"id": group_id, "items": items})

    workspace = next(
        (group for group in groups if group["id"] == "workspace"), None
    )
    if workspace is None:
        groups.insert(0, {"id": "workspace", "items": ["dashboard"]})
    else:
        workspace["items"] = [
            "dashboard",
            *[item for item in workspace["items"] if item != "dashboard"],
        ]
        groups = [workspace, *[group for group in groups if group is not workspace]]
    return {"groups": groups[:MAX_DOCK_GROUPS]}


def serialize_mobile_dock_config(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
