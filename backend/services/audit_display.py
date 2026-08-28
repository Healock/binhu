"""Human-readable labels for the immutable administrator audit trail."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


ACTION_LABELS: dict[str, str] = {
    "logs.export": "导出系统日志",
    "backup.create": "创建数据库备份",
    "backup.schedule.update": "修改备份计划",
    "backup.download": "下载数据库备份",
    "diagnostics.export": "导出诊断包",
    "account.password.change": "修改登录密码",
    "account.session.revoke": "下线指定登录设备",
    "account.session.revoke_others": "下线其他登录设备",
    "account.session.revoke_all": "下线全部登录设备",
    "account.avatar.update": "更换个人头像",
    "oauth.update": "更新腾讯文档授权",
    "oauth.test": "测试腾讯文档授权",
    "area.create": "新建片区",
    "area.update": "修改片区",
    "area.delete": "删除片区",
    "community.enable": "启用社区",
    "community.disable": "停用社区",
    "personnel.create_with_account": "新增人员和账号",
    "personnel.update": "修改人员资料",
    "personnel.delete_with_account": "删除人员和关联账号",
    "announcement.create": "发布公告",
    "announcement.delete": "删除公告",
    "permission_group.create": "新建权限组",
    "permission_group.update": "修改权限组",
    "permission_group.delete": "删除权限组",
    "permission_group.position_mapping.update": "修改岗位默认权限组",
    "personnel.weekend_duty.update": "修改双休日排班",
    "police_address.create": "新增小区地址",
    "police_address.update": "修改小区地址",
    "police_address.disable": "停用小区地址",
    "police_address.delete": "删除小区地址",
    "police_address.export": "导出小区地址",
    "police_address.import": "导入小区地址映射（历史功能）",
    "police_dispatch.import": "导入下发数据",
    "police_dispatch.preview": "预览已处理下发数据",
    "police_dispatch.import_preview": "预览业务数据导入",
    "police_dispatch.import_confirm": "确认业务数据导入",
    "police_dispatch.quick_create": "快捷下发临时任务",
    "police_dispatch.delete": "删除下发批次",
    "police_dispatch.business_fields.update": "修改下发任务字段",
    "police_dispatch.review": "审核下发任务",
    "police_dispatch.duplicate.resolve": "选择下发重复记录",
    "police_dispatch.bulk_review": "批量审核下发任务",
    "police_dispatch.conflict.adopt_tencent": "采用腾讯表格内容",
    "police_dispatch.conflict.overwrite_tencent": "用平台内容覆盖腾讯表格",
    "police_dispatch.publish": "发布下发任务",
    "fullchain.police_raw.confirm": "确认全链条公安网原始数据",
    "fullchain.archive.review": "审核全链条归档候选",
    "fullchain.archive.export": "导出并归档全链条任务",
    "online.writeback.update": "修改腾讯原始行",
    "online.writeback.queue": "保存平台数据并排队同步",
    "online.writeback.resolve_conflict": "处理腾讯同步冲突",
    "online.writeback.create": "新增腾讯原始行",
    "online.writeback.delete": "删除腾讯原始行",
    "spreadsheet.config.update": "批量修改在线表格配置",
    "spreadsheet.create": "新增在线表格配置",
    "spreadsheet.update": "修改在线表格配置",
    "spreadsheet.delete": "删除在线表格配置",
    "report.summary_config.update": "修改总汇总配置",
    "sync.trigger": "触发在线数据同步",
    "sync.schedule.update": "修改自动同步计划",
    "system.config.update": "修改系统设置",
    "task_graph.config.update": "修改个人任务图设置",
    "task_graph.backfill": "回填个人任务依赖图",
    "user.create": "创建用户",
    "user.update": "修改用户",
    "user.password.reset": "管理员重置用户密码",
    "user.delete": "删除用户",
    "visit_detail.import": "导入走访明细",
    "visit_rating.import": "导入星级评定",
    "visit_source.preview": "预览走访与星级来源数据",
    "visit_source.confirm": "确认走访与星级来源数据",
    "code_summary.fetch": "获取平安码与管家码数据",
    "code_summary.location_classify": "维护码数据位置分类",
    "code_summary.location_recompute": "重新计算码数据位置汇总",
    "qmf_status_scan.start": "启动全民防反馈扫描",
    "qmf_source.sync": "同步全民防未核查任务",
    "qmf.self_owned.import": "导入辖区自购自住人员资产资料",
    "residence_platform.config.update": "修改居住证平台只读查询配置",
    "residence_platform.login": "登录居住证平台只读查询",
    "residence_platform.scan.start": "启动居住证登记资料查询",
    "venue.create": "创建场所码",
    "venue.update": "修改场所码",
    "venue.rotate_token": "轮换场所码二维码",
    "venue.export": "导出场所登记记录",
    "work_log.create": "创建工作日志",
    "work_log.delete": "删除工作日志",
    "work_log.takeover": "接管工作日志",
    "work_log.refresh": "刷新工作日志系统数据",
    "work_log.export": "导出工作日志",
    "work_log_detail.export": "导出工作每日明细",
    "online_summary.export": "导出在线数据汇总",
    "visit_summary.export": "导出走访汇总",
    "code_summary.export": "导出平安码与管家码汇总",
    "police_dispatch.feedback.export": "导出下发反馈表",
    "mobile_tasks.bulk_assign": "批量分配核查人",
    "mobile_tasks.self_claim": "自主领取核查任务",
    "mobile_tasks.registration.manual_confirm": "人工确认登记结果",
    "qmf_registration.preview": "全民防模型三只读预演",
    "qmf_registration.config.update": "修改全民防封闭测试配置",
    "qmf_registration.prepare": "准备全民防模型三真实登记",
    "qmf_registration.status.read": "查询全民防模型三反馈状态",
    "qmf_registration.execute": "执行全民防模型三真实登记",
    "qmf_registration.tencent_marker.retry": "重试全民防腾讯完成标记",
    "registry.household_import.preview": "预览户号表导入",
    "registry.household_import.confirm": "确认户号表导入",
    "registry.certificate_import.preview": "预览房东责任告知书导入",
    "registry.certificate_import.confirm": "确认房东责任告知书导入",
    "registry.certificate_image.view": "查看房东责任告知书图片",
    "registry.import_issue.bulk_create": "创建档案导入问题记录",
    "registry.import_issue.review": "核查档案导入问题记录",
    "registry.property.create": "辖区房屋档案创建",
    "registry.property.update": "修改辖区房屋档案",
    "registry.property.status": "修改辖区房屋状态",
    "registry.property_person.attach": "关联房屋人员",
    "registry.property_person.update": "修改房屋人员关系",
    "registry.property_organization.attach": "关联房屋机构",
    "registry.property_organization.update": "修改房屋机构关系",
    "registry.alias.create": "新增房屋地址别名",
    "registry.alias.status": "修改地址别名状态",
    "registry.person.create": "辖区人员档案创建",
    "registry.person.update": "修改辖区人员档案",
    "registry.person.tag.create": "从人员档案新增标签",
    "registry.person.tag.release": "从人员档案解除标签",
    "registry.person.merge": "合并辖区人员档案",
    "registry.person.merge_undo": "撤销辖区人员合并",
    "registry.phone.create": "新增辖区人员联系电话",
    "registry.organization.create": "新增机构档案",
    "registry.organization.update": "修改机构档案",
    "registry.organization_member.attach": "关联机构经办人",
    "registry.organization_member.update": "修改机构经办人关系",
    "registry.candidate.review": "审核辖区档案候选变更",
    "registry.conflict.review": "处理辖区档案冲突",
    "registry.watch_category.create": "人员标签分类创建",
    "registry.watch_category.update": "修改人员标签分类",
    "registry.watch_person.create": "人员标签档案创建",
    "registry.watch_person.update": "修改人员标签档案",
    "registry.watch_assignment.create": "新增人员标签",
    "registry.watch_assignment.update": "修改人员标签",
    "registry.watch_import.preview": "预览人员标签名单",
    "registry.watch_import.confirm": "确认导入人员标签名单",
    "workflow.version.create": "保存工单流程草稿",
    "workflow.version.update": "修改工单流程草稿",
    "workflow.version.publish": "发布工单流程版本",
    "workflow.ticket.transfer": "转派工单",
    "workflow.ticket.claim": "领取工单",
    "workflow.ticket.decision": "处理工单",
    "workflow.ticket.supplement": "补充工单材料",
    "workflow.ticket.restore_queued": "恢复工单为待领取",
    "workflow.ticket.withdraw": "撤回或取消工单",
    "workflow.attachment.upload": "上传工单附件",
    "workflow.attachment.delete": "删除工单附件",
    "workflow.photo_import.preview": "预览照片调取批次",
    "workflow.photo_import.confirm": "确认照片调取批次",
    "workflow.photo_import.reconcile": "修复照片批次遗漏工单",
    "workflow.photo_requests.export": "导出未调照片清单",
    "workflow.photo_requests.batch_claim": "批量领取照片工单",
    "workflow.photo_sheet.config": "修改调照片名单配置",
    "workflow.photo_sheet.preview": "预览调照片历史名单",
    "workflow.photo_sheet.import": "导入调照片历史名单",
    "workflow.photo_sheet.sync": "同步调照片名单",
    "workflow.photo_sheet.conflict_retry": "重试调照片名单冲突",
    "workflow.photo_sheet.outbox_retry": "重新执行调照片名单写回",
}

TARGET_TYPE_LABELS: dict[str, str] = {
    "photo_sheet": "调照片名单",
    "photo_sheet_conflict": "调照片名单冲突",
    "photo_sheet_outbox": "调照片名单写回任务",
    "work_log_daily_detail": "工作每日明细",
    "container": "系统日志",
    "backup": "数据库备份",
    "backup_schedule": "备份计划",
    "system": "系统",
    "user": "用户账号",
    "oauth": "腾讯文档授权",
    "area": "片区",
    "community": "社区",
    "grid_member": "人员",
    "announcement": "公告",
    "permission_group": "权限组",
    "weekend_duty": "双休日排班",
    "police_address": "小区地址",
    "police_address_import": "小区地址导入批次",
    "police_dispatch_batch": "数据下发批次",
    "police_dispatch_import": "业务数据导入",
    "police_dispatch_preview": "数据下发预览",
    "police_dispatch_publish_run": "数据下发后台发布任务",
    "police_dispatch_task": "数据下发任务",
    "fullchain_police_raw_upload": "全链条公安网原始数据",
    "fullchain_archive_export": "全链条反馈归档批次",
    "mobile_task": "流口核查任务",
    "online_task": "在线核查任务",
    "qmf_registration_run": "全民防登记运行",
    "qmf_status_scan": "全民防反馈扫描",
    "qmf_source": "全民防同步",
    "qmf_self_owned_batch": "辖区自购自住人员资产资料",
    "venue": "场所码",
    "venue_visits": "场所登记记录",
    "external_session": "外部平台只读会话",
    "external_readonly_scan": "外部平台只读查询",
    "online_source_row": "腾讯原始行",
    "spreadsheet": "在线表格配置",
    "system_config": "系统设置",
    "sync": "同步任务",
    "sync_schedule": "自动同步计划",
    "visit_import": "走访导入批次",
    "visit_source": "走访来源获取",
    "code_summary": "平安码与管家码汇总",
    "code_summary_location": "码数据采集位置",
    "work_log_draft": "工作日志",
    "online_summary": "在线数据汇总",
    "visit_summary": "走访汇总",
    "registry_property": "辖区房屋档案",
    "registry_source_batch": "档案导入批次",
    "registry_certificate_source_run": "告知书读取任务",
    "registry_property_certificate": "房东责任告知书",
    "registry_import_issue": "档案导入问题",
    "registry_property_person_role": "房屋人员关系",
    "registry_property_organization_role": "房屋机构关系",
    "registry_address_alias": "房屋地址别名",
    "registry_housing_person": "辖区人员档案",
    "registry_person_phone": "辖区人员联系电话",
    "registry_organization": "机构档案",
    "registry_organization_membership": "机构经办人关系",
    "registry_change_candidate": "档案候选变更",
    "registry_conflict": "档案冲突",
    "registry_merge": "人员档案合并",
    "watch_category": "人员标签分类",
    "watch_person": "人员标签档案",
    "watch_assignment": "人员标签",
    "workflow_version": "工单流程版本",
    "work_order": "工单",
    "work_order_attachment": "工单附件",
    "photo_import_batch": "照片调取批次",
    "task_graph": "个人任务依赖图",
}

RESULT_LABELS: dict[str, str] = {
    "success": "成功",
    "completed": "成功",
    "partial": "部分成功",
    "failed": "失败",
    "denied": "已拒绝",
    "duplicate": "重复文件",
    "conflict": "发生冲突",
    "pending": "等待处理",
    "running": "处理中",
}

DETAIL_LABELS: dict[str, str] = {
    "since_minutes": "日志时间范围",
    "bytes": "导出大小",
    "filename": "文件名",
    "enabled": "启用状态",
    "is_active": "启用状态",
    "run_hour": "执行小时",
    "run_minute": "执行分钟",
    "retention_days": "保留天数",
    "temporary_password_cleared": "临时密码状态已清除",
    "configured": "是否已配置",
    "http_status": "接口状态码",
    "error": "错误原因",
    "area_id": "片区编号",
    "leader_count": "片长人数",
    "position": "岗位",
    "department_count": "所属部门数",
    "account_id": "账号编号",
    "account_mode": "账号处理方式",
    "identity_added": "已登记身份证号",
    "changed_fields": "变更字段",
    "identity_changed": "身份证号已变更",
    "account_changed": "关联账号已变更",
    "account_swapped": "已交换关联账号",
    "affected_account_ids": "受影响账号",
    "account_deleted": "关联账号已删除",
    "title": "标题",
    "severity": "级别",
    "name": "名称",
    "permission_count": "权限数量",
    "affected_users": "受影响账号数",
    "member_count": "人员数量",
    "complete": "排班是否完整",
    "address_type": "地址类型",
    "row_count": "数据行数",
    "accepted": "导入数量",
    "conflicts": "冲突数量",
    "import_kind": "导入类型",
    "change_digest": "变更摘要",
    "affected_count": "受影响记录数",
    "action": "处理结果",
    "batch_id": "批次编号",
    "count": "处理数量",
    "mode": "处理方式",
    "row_hash": "来源行版本",
    "success": "成功数量",
    "failed": "失败数量",
    "source_id": "来源行编号",
    "run_id": "登记运行编号",
    "result_code": "结果代码",
    "duration_ms": "耗时（毫秒）",
    "tencent_marker_status": "腾讯标记状态",
    "photo": "照片校验摘要",
    "mime_type": "图片类型",
    "size_bytes": "图片大小（字节）",
    "sha256": "图片摘要",
    "columns": "修改字段",
    "parser_type": "业务类型",
    "types": "业务类型",
    "fields": "修改字段",
    "keys": "设置项目",
    "organization_prefix": "机构代码前缀",
    "queued_count": "待查询任务数",
    "interval_minutes": "同步间隔",
    "member_id": "关联人员编号",
    "assignment_mode": "权限组来源",
    "permission_groups": "权限组",
    "temporary_password": "使用临时密码",
    "password_changed": "密码已修改",
    "sessions_invalidated": "原登录会话已失效",
    "revoked_sessions": "下线会话数",
    "includes_current_session": "包含当前设备",
    "replaced_existing": "替换已有头像",
    "inserted_rows": "新增行数",
    "updated_rows": "更新行数",
    "unchanged_rows": "未变化行数",
    "ignored_rows": "忽略行数",
    "error_count": "错误数量",
    "warning_count": "警告数量",
    "unmatched_rows": "未匹配行数",
    "ambiguous_rows": "多义匹配行数",
    "duplicate_file": "重复文件",
    "reason": "原因",
    "report_type": "日志类型",
    "business_date": "业务日期",
    "previous_owner": "原负责人",
    "version": "数据版本",
    "missing_count": "未填写字段数",
    "file_format": "文件格式",
    "summary_type": "汇总类型",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "inspector_rows": "人员明细行数",
    "community_rows": "社区汇总行数",
    "community_id": "社区编号",
    "has_identity": "已登记身份证号",
    "has_end": "已设置结束时间",
    "type": "类型",
    "active": "启用状态",
    "status": "状态",
    "target_id": "目标档案编号",
    "backfilled": "历史回填数量",
    "step_count": "流程节点数量",
}

VALUE_LABELS: dict[str, str] = {
    "apartment": "公寓",
    "community": "居民小区",
    "construction_dormitory": "工地宿舍",
    "existing": "关联已有账号",
    "create": "同时创建账号",
    "inherited": "继承岗位默认权限组",
    "custom": "账号自定义权限组",
    "dispatch": "下发到社区",
    "no_registration": "无需登记",
    "transfer": "移交",
    "duplicate": "重复排除",
    "duplicate_exclude": "重复排除",
    "accept_suggestion": "采用平台建议",
    "set_action": "统一指定处理结果",
    "adopt_tencent": "采用腾讯内容",
    "overwrite_tencent": "采用平台内容",
    "detail": "走访明细",
    "rating": "星级评定",
    "daily": "日报",
    "weekly": "周报",
    "monthly": "月报",
    "info": "普通",
    "warning": "重要",
    "error": "紧急",
    "other": "其他",
}

FIELD_LABELS: dict[str, str] = {
    "id_card_number": "身份证号",
    "phone": "手机号",
    "community": "社区",
    "department_id": "部门",
    "department_ids": "所属部门",
    "permission_group_ids": "权限组",
    "display_name": "显示姓名",
    "member_id": "关联人员",
    "group_assignment_mode": "权限组来源",
    "password": "密码",
    "is_active": "启用状态",
    "enabled": "启用状态",
    "name": "名称",
    "url": "腾讯文档地址",
    "file_id": "腾讯文件编号",
    "data_sheet_id": "数据工作表编号",
    "summary_sheet_id": "汇总工作表编号",
    "header_row": "表头行",
    "parser_type": "业务类型",
    "position": "岗位",
    "notes": "备注",
    "status": "状态",
    "username": "用户名",
    "account_id": "关联账号",
    "timezone": "系统时区",
    "visit_summary_positions": "出租房走访统计岗位",
    "weekend_duty_positions": "双休日备勤岗位",
    "permission_enforcement_enabled": "权限强制开关",
    "online_writeback_enabled": "在线回写总开关",
}

TARGET_NAME_LABELS: dict[str, str] = {
    "daily": "每日备份计划",
    "default": "默认同步计划",
    "tencent-docs": "腾讯文档",
    "operations-center": "运维中心",
    "batch": "批量配置",
    "position_mappings": "岗位默认权限组",
    "summary_types": "总汇总业务类型",
    "qmf_closed_test": "全民防模型三封闭测试",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action or "未知操作")


def result_label(result: str) -> str:
    return RESULT_LABELS.get(result, result or "未知")


def actor_name(
    *,
    member_name: str | None,
    display_name: str | None,
    current_username: str | None,
    recorded_username: str,
    user_id: int | None,
) -> str:
    return next(
        (
            value.strip()
            for value in (
                member_name,
                display_name,
                current_username,
                recorded_username,
            )
            if value and value.strip()
        ),
        "系统自动任务" if user_id is None else "已删除账号",
    )


def actor_account(current_username: str | None, recorded_username: str) -> str:
    return (current_username or recorded_username or "").strip()


def target_display(target_type: str, target_name: str) -> str:
    type_label = TARGET_TYPE_LABELS.get(target_type, target_type or "未指定目标")
    raw_name = (target_name or "").strip()
    if not raw_name:
        return type_label
    name = TARGET_NAME_LABELS.get(raw_name, raw_name)
    if name == raw_name and raw_name.isdigit():
        name = f"#{raw_name}"
    elif target_type == "online_source_row" and ":" in raw_name:
        parser_type, source_id = raw_name.rsplit(":", 1)
        name = f"{parser_type} · 来源行 #{source_id}"
    return f"{type_label} · {name}"


def _field_label(value: Any) -> str:
    text = str(value)
    return FIELD_LABELS.get(text, DETAIL_LABELS.get(text, text))


def _format_scalar(key: str, value: Any) -> str:
    if value is None:
        return "未填写"
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value)
    if key in {"row_hash", "change_digest", "sha256"} and len(text) > 16:
        return f"{text[:12]}…"
    return VALUE_LABELS.get(text, RESULT_LABELS.get(text, text))


def format_detail_value(key: str, value: Any) -> str:
    if isinstance(value, Mapping):
        return "；".join(
            f"{DETAIL_LABELS.get(str(child_key), str(child_key))}："
            f"{format_detail_value(str(child_key), child_value)}"
            for child_key, child_value in value.items()
        ) or "无"
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        formatter = (
            _field_label
            if key in {"changed_fields", "columns", "fields", "keys"}
            else str
        )
        return "、".join(formatter(item) for item in value) or "无"
    if key == "since_minutes":
        return f"最近 {_format_scalar(key, value)} 分钟"
    if key == "bytes":
        try:
            size = int(value)
        except (TypeError, ValueError):
            return _format_scalar(key, value)
        if size >= 1024 * 1024:
            return f"{size / 1024 / 1024:.1f} MB"
        if size >= 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size} B"
    if key in {"run_hour"}:
        return f"{_format_scalar(key, value)} 时"
    if key in {"run_minute"}:
        return f"{_format_scalar(key, value)} 分"
    if key in {"retention_days"}:
        return f"{_format_scalar(key, value)} 天"
    if key in {"interval_minutes"}:
        return f"{_format_scalar(key, value)} 分钟"
    return _format_scalar(key, value)


def detail_items(detail: Any) -> list[dict[str, str]]:
    if detail is None:
        return []
    if not isinstance(detail, Mapping):
        return [
            {
                "key": "detail",
                "label": "详情",
                "value": format_detail_value("detail", detail),
            }
        ]
    return [
        {
            "key": str(key),
            "label": DETAIL_LABELS.get(str(key), str(key)),
            "value": format_detail_value(str(key), value),
        }
        for key, value in detail.items()
    ]


def action_options() -> list[dict[str, str]]:
    return [
        {"value": value, "label": label}
        for value, label in sorted(ACTION_LABELS.items(), key=lambda item: item[1])
    ]
