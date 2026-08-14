"""账号级手机导航配置的校验与兼容处理。"""

from __future__ import annotations

import json
from typing import Any, Iterable


DEFAULT_MOBILE_NAVIGATION_MODE = "dock"
MAX_DOCK_GROUPS = 4
DOCK_CONFIG_VERSION = 2

GROUP_ITEMS: dict[str, tuple[str, ...]] = {
    "workspace": (
        "dashboard",
        "online_query",
        "data_upload",
        "work_log",
        "workflow_tickets",
    ),
    "tasks": (
        "flow_tasks",
        "police_tasks",
        "police_analysis",
        "photo_tasks",
    ),
    "summaries": (
        "online_summary",
        "visit_summary",
    ),
    "resources": (
        "grid_members",
        "communities",
        "police_addresses",
        "registry",
        "watch_people",
        "users",
        "permission_groups",
    ),
    "system": (
        "settings",
        "workflow_config",
        "operations",
    ),
}

SUPER_ADMIN_ITEMS = {"users", "permission_groups", "operations"}
ADMIN_ITEMS = {"data_upload", "work_log", "police_addresses"}

ITEM_PERMISSIONS: dict[str, str] = {
    "online_summary": "online.summary.view",
    "online_query": "online.raw.view",
    "flow_tasks": "online.raw.view",
    "police_tasks": "police.dispatch.manage",
    "police_analysis": "online.task.manage",
    "photo_tasks": "workflow.ticket.handle",
    "workflow_tickets": "workflow.ticket.view",
    "visit_summary": "visit.summary.view",
    "data_upload": "visit.import",
    "work_log": "worklog.manage",
    "grid_members": "personnel.basic.view",
    "communities": "community.view",
    "police_addresses": "police.address.manage",
    "registry": "registry.property.view",
    "watch_people": "registry.watch.view",
    "users": "user.manage",
    "permission_groups": "permission.manage",
    "workflow_config": "workflow.config.manage",
    "operations": "ops.manage",
}

ITEM_PERMISSION_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "data_upload": ("visit.import", "police.dispatch.manage"),
    "police_analysis": ("online.task.manage", "police.dispatch.manage"),
    "photo_tasks": ("workflow.ticket.handle", "workflow.ticket.manage"),
}


def normalize_mobile_navigation_mode(value: Any) -> str:
    return (
        str(value)
        if value in {"sidebar", "dock"}
        else DEFAULT_MOBILE_NAVIGATION_MODE
    )


def _admin_code_access(
    role: str,
    permission_group_codes: Iterable[str] | None,
) -> bool:
    return role in {"admin", "super_admin"} or bool(
        {str(code).strip() for code in permission_group_codes or []}
        & {"admin", "super_admin"}
    )


def _item_is_accessible(
    item_id: str,
    role: str,
    permissions: Iterable[str] | None = None,
    permission_group_codes: Iterable[str] | None = None,
    position: str | None = None,
) -> bool:
    admin_access = _admin_code_access(role, permission_group_codes)
    if item_id == "online_query":
        group_codes = {
            str(code).strip() for code in permission_group_codes or []
        }
        query_admin_access = bool(group_codes & {"admin", "super_admin"}) or (
            not group_codes and role in {"admin", "super_admin"}
        )
        if not query_admin_access:
            return False
    if item_id == "flow_tasks":
        has_task_manage = permissions is not None and "online.task.manage" in permissions
        if position not in {"组长", "组员"} and not has_task_manage and not admin_access:
            return False
    if item_id == "police_tasks":
        if position not in {"基础管控", "中队长"} and not (not position and admin_access):
            return False
    if item_id == "police_analysis":
        has_analysis_permission = permissions is not None and any(
            permission in permissions
            for permission in ITEM_PERMISSION_ALTERNATIVES["police_analysis"]
        )
        if not has_analysis_permission and not admin_access:
            return False
    if item_id == "photo_tasks":
        has_manage_permission = permissions is not None and "workflow.ticket.manage" in permissions
        if position != "基础管控" and not has_manage_permission and not admin_access:
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
    position: str | None = None,
) -> dict[str, Any]:
    """返回新版本默认 Dock；所有岗位按同一分类顺序取前四组。"""
    groups = []
    for group_id, item_ids in GROUP_ITEMS.items():
        items = [
            item_id
            for item_id in item_ids
            if _item_is_accessible(
                item_id,
                role,
                permissions,
                permission_group_codes,
                position,
            )
        ]
        if items:
            groups.append({"id": group_id, "items": items})
    return {"version": DOCK_CONFIG_VERSION, "groups": groups[:MAX_DOCK_GROUPS]}


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
    position: str | None = None,
) -> dict[str, Any]:
    """过滤配置；旧版本配置故意不迁移，直接返回 v2 默认值。"""
    parsed = _load_config(value)
    if not parsed or parsed.get("version") != DOCK_CONFIG_VERSION:
        return default_mobile_dock_config(
            role, permissions, permission_group_codes, position
        )
    raw_groups = parsed.get("groups")
    if not isinstance(raw_groups, list):
        return default_mobile_dock_config(
            role, permissions, permission_group_codes, position
        )

    raw_task_items = {
        str(item or "")
        for group in raw_groups
        if isinstance(group, dict)
        and str(group.get("id") or "") == "tasks"
        and isinstance(group.get("items"), list)
        for item in group["items"]
    }
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
                    item_id,
                    role,
                    permissions,
                    permission_group_codes,
                    position,
                )
            ):
                continue
            seen_items.add(item_id)
            items.append(item_id)
        if items:
            seen_groups.add(group_id)
            groups.append({"id": group_id, "items": items})

    accessible_defaults = default_mobile_dock_config(
        role, permissions, permission_group_codes, position
    )["groups"]
    if not groups:
        return {
            "version": DOCK_CONFIG_VERSION,
            "groups": accessible_defaults,
        }
    workspace = next((group for group in groups if group["id"] == "workspace"), None)
    if workspace is None:
        workspace = {"id": "workspace", "items": ["dashboard"]}
        groups.insert(0, workspace)
    else:
        workspace["items"] = [
            "dashboard",
            *[item for item in workspace["items"] if item != "dashboard"],
        ]
        groups = [workspace, *[group for group in groups if group is not workspace]]
    accessible_by_group = {
        group["id"]: set(group["items"])
        for group in accessible_defaults
    }
    if (
        "workflow_tickets" in raw_task_items
        and "workflow_tickets" in accessible_by_group.get("workspace", set())
        and "workflow_tickets" not in workspace["items"]
    ):
        workspace["items"].append("workflow_tickets")
    tasks = next((group for group in groups if group["id"] == "tasks"), None)
    if tasks is not None:
        for previous_id, next_id in (
            ("police_tasks", "police_analysis"),
            ("workflow_tickets", "photo_tasks"),
        ):
            if (
                previous_id in raw_task_items
                and next_id in accessible_by_group.get("tasks", set())
                and next_id not in tasks["items"]
            ):
                tasks["items"].append(next_id)
    present = {group["id"] for group in groups}
    for candidate in accessible_defaults:
        if len(groups) >= MAX_DOCK_GROUPS:
            break
        if candidate["id"] in present:
            continue
        groups.append(candidate)
        present.add(candidate["id"])
    return {"version": DOCK_CONFIG_VERSION, "groups": groups[:MAX_DOCK_GROUPS]}


def validate_mobile_dock_config(
    value: Any,
    role: str,
    permissions: Iterable[str] | None = None,
    permission_group_codes: Iterable[str] | None = None,
    position: str | None = None,
) -> dict[str, Any]:
    """保存配置前做严格校验，避免静默接受错误或越权入口。"""
    parsed = _load_config(value)
    if not parsed or parsed.get("version") != DOCK_CONFIG_VERSION:
        raise ValueError("Dock 配置版本过旧，请刷新后重试")
    raw_groups = parsed.get("groups")
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
                item_id,
                role,
                permissions,
                permission_group_codes,
                position,
            ):
                raise ValueError("Dock 配置包含当前账号无权访问的页面")
            seen_items.add(item_id)
            items.append(item_id)
        seen_groups.add(group_id)
        groups.append({"id": group_id, "items": items})

    workspace = next((group for group in groups if group["id"] == "workspace"), None)
    if workspace is None:
        groups.insert(0, {"id": "workspace", "items": ["dashboard"]})
    else:
        workspace["items"] = [
            "dashboard",
            *[item for item in workspace["items"] if item != "dashboard"],
        ]
        groups = [workspace, *[group for group in groups if group is not workspace]]
    present = {group["id"] for group in groups}
    for candidate in default_mobile_dock_config(
        role, permissions, permission_group_codes, position
    )["groups"]:
        if len(groups) >= MAX_DOCK_GROUPS:
            break
        if candidate["id"] in present:
            continue
        groups.append(candidate)
        present.add(candidate["id"])
    return {"version": DOCK_CONFIG_VERSION, "groups": groups[:MAX_DOCK_GROUPS]}


def serialize_mobile_dock_config(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
