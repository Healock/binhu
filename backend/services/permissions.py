"""权限组、岗位默认组和数据范围的统一定义。"""

from __future__ import annotations

import json
from typing import Any


ONLINE_SUMMARY_VIEW = "online.summary.view"
ONLINE_RAW_VIEW = "online.raw.view"
VISIT_SUMMARY_VIEW = "visit.summary.view"
PERSONNEL_BASIC_VIEW = "personnel.basic.view"
PERSONNEL_SENSITIVE_VIEW = "personnel.sensitive.view"
COMMUNITY_VIEW = "community.view"
NOTIFICATION_VIEW = "notification.view"
PREFERENCES_MANAGE = "preferences.manage"
SYNC_TRIGGER = "sync.trigger"
REPORT_CONFIG_MANAGE = "report.config.manage"
VISIT_IMPORT = "visit.import"
WORK_LOG_MANAGE = "worklog.manage"
ATTENDANCE_MANAGE = "attendance.manage"
PERSONNEL_MANAGE = "personnel.manage"
COMMUNITY_MANAGE = "community.manage"
USER_MANAGE = "user.manage"
PERMISSION_MANAGE = "permission.manage"
ANNOUNCEMENT_MANAGE = "announcement.manage"
SYSTEM_MANAGE = "system.manage"
OPS_MANAGE = "ops.manage"

PERMISSION_CATALOG = [
    (ONLINE_SUMMARY_VIEW, "数据查看", "查看在线数据汇总"),
    (ONLINE_RAW_VIEW, "数据查看", "查询在线原始及归档数据"),
    (VISIT_SUMMARY_VIEW, "数据查看", "查看走访概览和汇总"),
    (PERSONNEL_BASIC_VIEW, "基础资料", "查看人员基础信息"),
    (PERSONNEL_SENSITIVE_VIEW, "基础资料", "查看人员敏感资料和出勤原因"),
    (COMMUNITY_VIEW, "基础资料", "查看社区资料"),
    (NOTIFICATION_VIEW, "个人功能", "查看公告和个人提示"),
    (PREFERENCES_MANAGE, "个人功能", "修改个人设置和密码"),
    (SYNC_TRIGGER, "业务操作", "手动同步在线数据"),
    (REPORT_CONFIG_MANAGE, "业务操作", "修改总汇总表配置"),
    (VISIT_IMPORT, "业务操作", "上传走访和星级评定"),
    (WORK_LOG_MANAGE, "业务操作", "管理和导出工作日志"),
    (ATTENDANCE_MANAGE, "人员操作", "管理请假和双休日备勤"),
    (PERSONNEL_MANAGE, "人员操作", "添加、编辑和删除人员"),
    (COMMUNITY_MANAGE, "人员操作", "添加、编辑和删除社区"),
    (USER_MANAGE, "平台管理", "管理用户账号"),
    (PERMISSION_MANAGE, "平台管理", "管理权限组和岗位映射"),
    (ANNOUNCEMENT_MANAGE, "平台管理", "发布和删除公告"),
    (SYSTEM_MANAGE, "平台管理", "管理数据源、OAuth 和系统设置"),
    (OPS_MANAGE, "平台管理", "使用运维中心"),
]

ALL_PERMISSIONS = {item[0] for item in PERMISSION_CATALOG}

COMMON_VIEW_PERMISSIONS = {
    ONLINE_SUMMARY_VIEW,
    ONLINE_RAW_VIEW,
    VISIT_SUMMARY_VIEW,
    PERSONNEL_BASIC_VIEW,
    COMMUNITY_VIEW,
    NOTIFICATION_VIEW,
    PREFERENCES_MANAGE,
}

INTERNAL_BUSINESS_PERMISSIONS = COMMON_VIEW_PERMISSIONS | {
    PERSONNEL_SENSITIVE_VIEW,
    SYNC_TRIGGER,
    REPORT_CONFIG_MANAGE,
    VISIT_IMPORT,
    WORK_LOG_MANAGE,
    ATTENDANCE_MANAGE,
}

DEFAULT_PERMISSION_GROUPS: dict[str, dict[str, Any]] = {
    "flow_post": {
        "name": "流口岗",
        "description": "组长、组员和自购房岗位的默认权限",
        "data_scope": "own_department",
        "permissions": COMMON_VIEW_PERMISSIONS,
        "sort_order": 10,
    },
    "global_viewer": {
        "name": "全局查看组",
        "description": "片长默认权限，可查看全所业务数据",
        "data_scope": "all",
        "permissions": COMMON_VIEW_PERMISSIONS,
        "sort_order": 20,
    },
    "internal_business": {
        "name": "内勤业务组",
        "description": "中队长和基础管控默认权限",
        "data_scope": "all",
        "permissions": INTERNAL_BUSINESS_PERMISSIONS,
        "sort_order": 30,
    },
    "admin": {
        "name": "管理员",
        "description": "负责人员、社区和日常业务管理",
        "data_scope": "all",
        "permissions": INTERNAL_BUSINESS_PERMISSIONS
        | {PERSONNEL_MANAGE, COMMUNITY_MANAGE},
        "sort_order": 40,
    },
    "super_admin": {
        "name": "超级管理员",
        "description": "拥有平台全部权限",
        "data_scope": "all",
        "permissions": ALL_PERMISSIONS,
        "sort_order": 50,
    },
}

POSITION_DEFAULT_GROUP = {
    "组长": "flow_post",
    "组员": "flow_post",
    "自购房": "flow_post",
    "片长": "global_viewer",
    "中队长": "internal_business",
    "基础管控": "internal_business",
    "社区民警": "admin",
    "所队领导": "admin",
}

INTERNAL_POSITIONS = {"片长", "中队长", "基础管控", "所队领导"}
COMMUNITY_POSITIONS = {"组长", "组员", "社区民警"}


def serialize_permissions(values: set[str] | list[str]) -> str:
    return json.dumps(sorted(set(values) & ALL_PERMISSIONS), ensure_ascii=False)


def parse_permissions(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            value = []
    if not isinstance(value, list):
        return []
    return sorted({str(item) for item in value if str(item) in ALL_PERMISSIONS})


def legacy_permissions(role: str) -> tuple[list[str], str, str]:
    """权限正式启用前保持旧账号可用。"""
    if role == "super_admin":
        group = DEFAULT_PERMISSION_GROUPS["super_admin"]
        return sorted(group["permissions"]), "all", "super_admin"
    if role == "admin":
        group = DEFAULT_PERMISSION_GROUPS["admin"]
        return sorted(group["permissions"]), "all", "admin"
    # 旧 leader/member 尚未关联人员时，兼容模式继续保留原有查看范围。
    return sorted(COMMON_VIEW_PERMISSIONS), "all", "legacy_viewer"


def has_permission(user: dict[str, Any], permission: str) -> bool:
    return permission in set(user.get("permissions") or [])


def permitted_community(
    user: dict[str, Any],
    permission: str | None = None,
) -> str | None:
    scope = user.get("data_scope")
    if permission:
        scope = (user.get("permission_scopes") or {}).get(
            permission,
            scope,
        )
    if scope == "all":
        return None
    department = user.get("department") or {}
    if department.get("type") != "community":
        return ""
    return str(department.get("community_name") or department.get("name") or "")


def catalog_payload() -> list[dict[str, str]]:
    return [
        {"code": code, "category": category, "label": label}
        for code, category, label in PERMISSION_CATALOG
    ]
