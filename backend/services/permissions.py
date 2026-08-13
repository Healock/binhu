"""权限组、岗位默认组和数据范围的统一定义。"""

from __future__ import annotations

import json
from typing import Any


ONLINE_SUMMARY_VIEW = "online.summary.view"
ONLINE_RAW_VIEW = "online.raw.view"
ONLINE_RAW_EDIT = "online.raw.edit"
ONLINE_RAW_ROW_MANAGE = "online.raw.row_manage"
ONLINE_TASK_MANAGE = "online.task.manage"
VISIT_SUMMARY_VIEW = "visit.summary.view"
PERSONNEL_BASIC_VIEW = "personnel.basic.view"
PERSONNEL_SENSITIVE_VIEW = "personnel.sensitive.view"
COMMUNITY_VIEW = "community.view"
NOTIFICATION_VIEW = "notification.view"
PREFERENCES_MANAGE = "preferences.manage"
SYNC_TRIGGER = "sync.trigger"
REPORT_CONFIG_MANAGE = "report.config.manage"
VISIT_IMPORT = "visit.import"
VISIT_SOURCE_MANAGE = "visit.source.manage"
WORK_LOG_MANAGE = "worklog.manage"
ATTENDANCE_MANAGE = "attendance.manage"
PERSONNEL_MANAGE = "personnel.manage"
COMMUNITY_MANAGE = "community.manage"
USER_MANAGE = "user.manage"
PERMISSION_MANAGE = "permission.manage"
ANNOUNCEMENT_MANAGE = "announcement.manage"
SYSTEM_MANAGE = "system.manage"
OPS_MANAGE = "ops.manage"
POLICE_DISPATCH_MANAGE = "police.dispatch.manage"
POLICE_ADDRESS_MANAGE = "police.address.manage"
REGISTRY_PROPERTY_VIEW = "registry.property.view"
REGISTRY_PROPERTY_MANAGE = "registry.property.manage"
REGISTRY_WATCH_VIEW = "registry.watch.view"
REGISTRY_WATCH_MANAGE = "registry.watch.manage"
REGISTRY_IMPORT_MANAGE = "registry.import.manage"
WORKFLOW_TICKET_CREATE = "workflow.ticket.create"
WORKFLOW_TICKET_VIEW = "workflow.ticket.view"
WORKFLOW_TICKET_HANDLE = "workflow.ticket.handle"
WORKFLOW_TICKET_MANAGE = "workflow.ticket.manage"
WORKFLOW_CONFIG_MANAGE = "workflow.config.manage"
WORKFLOW_ATTACHMENT_VIEW = "workflow.attachment.view"

PERMISSION_CATALOG = [
    (ONLINE_SUMMARY_VIEW, "数据查看", "查看在线数据汇总"),
    (ONLINE_RAW_VIEW, "数据查看", "查询在线原始及归档数据"),
    (ONLINE_RAW_EDIT, "数据业务", "修改腾讯在线表格中的现有数据"),
    (ONLINE_RAW_ROW_MANAGE, "数据业务", "新增或删除腾讯在线表格原始行"),
    (ONLINE_TASK_MANAGE, "数据业务", "管理现有流口任务及研判"),
    (VISIT_SUMMARY_VIEW, "数据查看", "查看走访概览和汇总"),
    (PERSONNEL_BASIC_VIEW, "基础资料", "查看人员基础信息"),
    (PERSONNEL_SENSITIVE_VIEW, "基础资料", "查看人员敏感资料和出勤原因"),
    (COMMUNITY_VIEW, "基础资料", "查看社区资料"),
    (NOTIFICATION_VIEW, "个人功能", "查看公告和个人提示"),
    (PREFERENCES_MANAGE, "个人功能", "修改个人设置和密码"),
    (SYNC_TRIGGER, "业务操作", "手动同步在线数据"),
    (REPORT_CONFIG_MANAGE, "业务操作", "修改总汇总表配置"),
    (VISIT_IMPORT, "业务操作", "上传走访和星级评定"),
    (VISIT_SOURCE_MANAGE, "业务操作", "获取和确认走访、星级来源数据"),
    (WORK_LOG_MANAGE, "业务操作", "管理和导出工作日志"),
    (ATTENDANCE_MANAGE, "人员操作", "管理请假和双休日备勤"),
    (PERSONNEL_MANAGE, "人员操作", "添加、编辑和删除人员"),
    (COMMUNITY_MANAGE, "人员操作", "添加、编辑和删除社区"),
    (USER_MANAGE, "平台管理", "管理用户账号"),
    (PERMISSION_MANAGE, "平台管理", "管理权限组和岗位映射"),
    (ANNOUNCEMENT_MANAGE, "平台管理", "发布和删除公告"),
    (SYSTEM_MANAGE, "平台管理", "管理数据源、OAuth 和系统设置"),
    (OPS_MANAGE, "平台管理", "使用运维中心"),
    (POLICE_DISPATCH_MANAGE, "业务操作", "管理全链条数据预处理、审核和发布"),
    (POLICE_ADDRESS_MANAGE, "基础资料", "管理小区地址库"),
    (REGISTRY_PROPERTY_VIEW, "基础资料", "查看辖区人房档案"),
    (REGISTRY_PROPERTY_MANAGE, "基础资料", "维护辖区人房档案"),
    (REGISTRY_WATCH_VIEW, "基础资料", "查看人员标记"),
    (REGISTRY_WATCH_MANAGE, "基础资料", "维护人员标记"),
    (REGISTRY_IMPORT_MANAGE, "业务操作", "管理档案导入与待审核变更"),
    (WORKFLOW_TICKET_CREATE, "业务操作", "发起工单"),
    (WORKFLOW_TICKET_VIEW, "业务操作", "查看工单"),
    (WORKFLOW_TICKET_HANDLE, "业务操作", "处理工单"),
    (WORKFLOW_TICKET_MANAGE, "业务操作", "管理全所工单"),
    (WORKFLOW_CONFIG_MANAGE, "平台管理", "配置和发布工单流程"),
    (WORKFLOW_ATTACHMENT_VIEW, "业务操作", "查看工单附件"),
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
    WORKFLOW_TICKET_CREATE,
    WORKFLOW_TICKET_VIEW,
    WORKFLOW_ATTACHMENT_VIEW,
}

FLOW_POST_PERMISSIONS = COMMON_VIEW_PERMISSIONS | {ONLINE_RAW_EDIT}

GLOBAL_VIEW_PERMISSIONS = COMMON_VIEW_PERMISSIONS | {
    ONLINE_RAW_EDIT,
    ONLINE_TASK_MANAGE,
}

INTERNAL_BUSINESS_PERMISSIONS = COMMON_VIEW_PERMISSIONS | {
    ONLINE_RAW_EDIT,
    ONLINE_RAW_ROW_MANAGE,
    ONLINE_TASK_MANAGE,
    PERSONNEL_SENSITIVE_VIEW,
    SYNC_TRIGGER,
    REPORT_CONFIG_MANAGE,
    VISIT_IMPORT,
    VISIT_SOURCE_MANAGE,
    WORK_LOG_MANAGE,
    ATTENDANCE_MANAGE,
    POLICE_DISPATCH_MANAGE,
    POLICE_ADDRESS_MANAGE,
    REGISTRY_PROPERTY_VIEW,
    REGISTRY_PROPERTY_MANAGE,
    REGISTRY_WATCH_VIEW,
    REGISTRY_WATCH_MANAGE,
    REGISTRY_IMPORT_MANAGE,
    WORKFLOW_TICKET_HANDLE,
    WORKFLOW_ATTACHMENT_VIEW,
}

# 社区民警需要保留日常查看能力，但辖区档案和人员标记只读，
# 数据范围由其关联的社区部门决定，不能复用 admin 组的全所维护权限。
COMMUNITY_REGISTRY_VIEW_PERMISSIONS = COMMON_VIEW_PERMISSIONS | {
    ONLINE_TASK_MANAGE,
    REGISTRY_PROPERTY_VIEW,
    REGISTRY_WATCH_VIEW,
}

DEFAULT_PERMISSION_GROUPS: dict[str, dict[str, Any]] = {
    "flow_post": {
        "name": "流口岗",
        "description": "组长、组员和自购房岗位的默认权限",
        "data_scope": "own_department",
        "permissions": FLOW_POST_PERMISSIONS,
        "sort_order": 10,
    },
    "community_address_manager": {
        "name": "本社区小区管理组",
        "description": "组长、组员维护和导出本人所属社区的小区资料",
        "data_scope": "own_department",
        "permissions": {POLICE_ADDRESS_MANAGE},
        "sort_order": 15,
    },
    "global_viewer": {
        "name": "全局查看组",
        "description": "片长默认权限，可查看全所业务数据",
        "data_scope": "all",
        "permissions": GLOBAL_VIEW_PERMISSIONS,
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
        | {PERSONNEL_MANAGE, COMMUNITY_MANAGE, WORKFLOW_TICKET_MANAGE},
        "sort_order": 40,
    },
    "community_registry_viewer": {
        "name": "社区档案查看组",
        "description": "社区民警只读查看所关联社区的人房档案和人员标记",
        "data_scope": "own_department",
        "permissions": COMMUNITY_REGISTRY_VIEW_PERMISSIONS,
        "sort_order": 25,
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
    "社区民警": "community_registry_viewer",
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
    communities = permitted_communities(user, permission)
    if communities is None:
        return None
    return communities[0] if communities else ""


def permitted_communities(
    user: dict[str, Any],
    permission: str | None = None,
) -> list[str] | None:
    scope = user.get("data_scope")
    if permission:
        scope = (user.get("permission_scopes") or {}).get(
            permission,
            scope,
        )
    if scope == "all":
        return None
    departments = user.get("departments")
    if not isinstance(departments, list):
        department = user.get("department") or {}
        departments = [department] if department else []
    result: list[str] = []
    for department in departments:
        if not isinstance(department, dict) or department.get("type") != "community":
            continue
        name = str(
            department.get("community_name") or department.get("name") or ""
        ).strip()
        if name and name not in result:
            result.append(name)
    return result


def catalog_payload() -> list[dict[str, str]]:
    return [
        {"code": code, "category": category, "label": label}
        for code, category, label in PERMISSION_CATALOG
    ]
