"""工作日志日报的字段、句子和表格结构。

模板结构由后端统一提供给网页和 PDF，避免两处版式逐渐不一致。
字段 ID 会写入草稿，已经发布后不要随意改名。
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable


TEMPLATE_VERSION = "daily-v2"
REPORT_TYPES = [
    {"value": "daily", "label": "日报", "enabled": True},
    {"value": "weekly", "label": "周报", "enabled": False, "hint": "等待模板"},
    {"value": "monthly", "label": "月报", "enabled": False, "hint": "等待模板"},
]


def field(
    field_id: str,
    label: str,
    field_type: str = "number",
    *,
    source: str = "manual",
    required: bool = True,
    source_key: str = "",
    width: int = 88,
    precision: int | None = None,
) -> dict:
    result = {
        "id": field_id,
        "label": label,
        "type": field_type,
        "source": source,
        "required": required,
        "width": width,
    }
    if source_key:
        result["source_key"] = source_key
    if precision is not None:
        result["precision"] = precision
    return result


def sentence(
    *segments: str | dict,
    title: str = "",
    style: str = "",
) -> dict:
    result = {"type": "sentence", "segments": list(segments)}
    if title:
        result["title"] = title
    if style:
        result["style"] = style
    return result


def text_block(
    field_id: str,
    label: str,
    *,
    required: bool = False,
    rows: int = 3,
) -> dict:
    return {
        "type": "textarea",
        "field": field(
            field_id,
            label,
            "textarea",
            required=required,
            width=320,
        ),
        "rows": rows,
    }


def heading(
    title: str,
    level: int = 2,
    *,
    combine_with_next: bool = True,
) -> dict:
    return {
        "type": "heading",
        "title": title,
        "level": level,
        "combine_with_next": combine_with_next,
    }


def column(
    key: str,
    label: str,
    field_type: str = "number",
    *,
    width: int = 96,
    required: bool = True,
) -> dict:
    return {
        "key": key,
        "label": label,
        "type": field_type,
        "width": width,
        "required": required,
    }


def group(label: str, children: list[dict]) -> dict:
    return {"label": label, "children": children}


def table(
    field_id: str,
    title: str,
    columns: list[dict],
    *,
    source: str = "manual",
    source_key: str = "",
    row_mode: str = "detail",
    community_key: str = "responsibility_area",
    fixed_rows: list[dict] | None = None,
    required: bool = True,
    help_text: str = "",
) -> dict:
    result = {
        "type": "table",
        "field": {
            **field(
                field_id,
                title,
                "table",
                source=source,
                required=required,
                source_key=source_key,
            ),
            "columns": columns,
            "row_mode": row_mode,
            "community_key": community_key,
        },
    }
    if fixed_rows:
        result["field"]["fixed_rows"] = fixed_rows
    if help_text:
        result["help"] = help_text
    return result


def rank_fields(prefix: str, label: str) -> list[str | dict]:
    return [
        f"{label}后三位：",
        field(f"{prefix}.1", "第一位", "text", width=92),
        "、",
        field(f"{prefix}.2", "第二位", "text", width=92),
        "、",
        field(f"{prefix}.3", "第三位", "text", width=92),
        "。",
    ]


def report_date_segments(
    prefix: str = "",
    suffix: str = "",
) -> list[str | dict]:
    return [
        prefix,
        field(
            "meta.month",
            "月份",
            source="system",
            source_key="calendar",
            width=64,
        ),
        "月",
        field(
            "meta.day",
            "日期",
            source="system",
            source_key="calendar",
            width=64,
        ),
        f"日{suffix}",
    ]


def report_full_date_segments(
    prefix: str = "",
    suffix: str = "",
) -> list[str | dict]:
    return [
        prefix,
        field(
            "meta.year",
            "年份",
            source="system",
            source_key="calendar",
            width=72,
        ),
        "年",
        *report_date_segments("", suffix),
    ]


COMMUNITY_OFFICER = [
    column("responsibility_area", "责任区", "text", width=86),
    column("community_officer", "社区民警", "text", width=96),
]

SCHEMA = {
    "template_version": TEMPLATE_VERSION,
    "report_types": REPORT_TYPES,
    "document_title": "滨湖新城派出所社区警务工作日志",
    "sections": [
        {
            "id": "flow",
            "title": "一、流动人口采集",
            "blocks": [
                heading("基础数据", 2),
                sentence(
                    *report_date_segments("截至", "24时，辖区实有人口总数"),
                    field("flow.population.total", "实有人口总数"),
                    "人，其中户籍人口",
                    field("flow.population.registered", "户籍人口"),
                    "人、流动人口",
                    field("flow.population.floating", "流动人口"),
                    "人。",
                ),
                heading("1. 登记注销", 3),
                sentence(
                    *report_date_segments("", "，流动人口新增"),
                    field("flow.registration.added", "新增"),
                    "人、主动注销",
                    field("flow.registration.active_cancelled", "主动注销"),
                    "人、被动注销",
                    field("flow.registration.passive_cancelled", "被动注销"),
                    "人。",
                ),
                table(
                    "flow.registration_table",
                    "流动人口登记注销统计",
                    [
                        *COMMUNITY_OFFICER,
                        column("grid_member_count", "网格员数"),
                        column("floating_total", "流动人口总数"),
                        column("added", "新增"),
                        column("added_rate", "占流口比率", "percent"),
                        column("active_cancelled", "主动注销"),
                        column("active_cancelled_rate", "占流口比率", "percent"),
                        column("passive_cancelled", "被动注销"),
                        column("passive_cancelled_rate", "占注销比率", "percent"),
                        column("average_workload", "人均工作量", "decimal"),
                        column("workload_rate", "占流口比率", "percent"),
                    ],
                    row_mode="community",
                ),
                sentence(*rank_fields(
                    "flow.analysis.workload_bottom",
                    "人均工作量比率",
                ), style="analysis"),
                sentence(*rank_fields(
                    "flow.analysis.added_bottom",
                    "新增比率",
                ), style="analysis"),
                sentence(*rank_fields(
                    "flow.analysis.active_cancelled_bottom",
                    "主动注销比率",
                ), style="analysis"),
                sentence(
                    "被动注销占比前三位：",
                    field(
                        "flow.analysis.passive_cancelled_top.1",
                        "第一位",
                        "text",
                        width=92,
                    ),
                    "、",
                    field(
                        "flow.analysis.passive_cancelled_top.2",
                        "第二位",
                        "text",
                        width=92,
                    ),
                    "、",
                    field(
                        "flow.analysis.passive_cancelled_top.3",
                        "第三位",
                        "text",
                        width=92,
                    ),
                    "。",
                    style="analysis",
                ),
                heading("2. 指令核查", 3),
                sentence(
                    "网格员人均核查指令数据",
                    field(
                        "flow.instruction.average_checked",
                        "人均核查数",
                        "decimal",
                        source="derived",
                        source_key="online_summary",
                    ),
                    "条；通过核查指令新增",
                    field("flow.instruction.added", "新增"),
                    "人，占比",
                    field(
                        "flow.instruction.added_rate",
                        "新增占比",
                        "percent",
                    ),
                    "；主动注销",
                    field("flow.instruction.active_cancelled", "主动注销"),
                    "人，占比",
                    field(
                        "flow.instruction.active_cancelled_rate",
                        "主动注销占比",
                        "percent",
                    ),
                    "。",
                ),
                sentence(
                    "未完成批次指令总数",
                    field(
                        "flow.instruction.total",
                        "数据总数",
                        source="derived",
                        source_key="online_summary",
                    ),
                    "条，已核查",
                    field(
                        "flow.instruction.checked",
                        "已核查",
                        source="derived",
                        source_key="online_summary",
                    ),
                    "条，未核查",
                    field(
                        "flow.instruction.unchecked",
                        "未核查",
                        source="derived",
                        source_key="online_summary",
                    ),
                    "条，完成率",
                    field(
                        "flow.instruction.completion_rate",
                        "完成率",
                        "percent",
                        source="derived",
                        source_key="online_summary",
                    ),
                    "；无法见底",
                    field(
                        "flow.instruction.unable",
                        "无法见底数",
                        source="derived",
                        source_key="online_summary",
                    ),
                    "条，见底率",
                    field(
                        "flow.instruction.ground_rate",
                        "见底率",
                        "percent",
                        source="derived",
                        source_key="online_summary",
                    ),
                    "。",
                ),
                table(
                    "flow.instruction_table",
                    "指令核查统计",
                    [
                        *COMMUNITY_OFFICER,
                        column("grid_member_count", "网格员数"),
                        column("total", "数据总数"),
                        column("unchecked", "未核查"),
                        column("checked", "已核查"),
                        column("completion_rate", "核查完成率", "percent"),
                        column("unable", "无法见底数"),
                        column("ground_rate", "核查见底率", "percent"),
                        column("average_checked", "当日人均核查数", "decimal"),
                    ],
                    source="system",
                    source_key="online_summary",
                    row_mode="system",
                    help_text="读取所选日期在线数据的总汇总表，固定采用两列口径。",
                ),
                sentence(*rank_fields(
                    "flow.instruction.completion_bottom",
                    "核查完成率",
                ), style="analysis"),
                sentence(*rank_fields(
                    "flow.instruction.ground_bottom",
                    "核查见底率",
                ), style="analysis"),
            ],
        },
        {
            "id": "rental",
            "title": "二、出租房屋管理",
            "blocks": [
                sentence(
                    *report_date_segments("截至", "24时，辖区房屋总数"),
                    field("rental.stock.total", "房屋总数"),
                    "户，其中自住",
                    field("rental.stock.self_occupied", "自住房"),
                    "户、出租",
                    field("rental.stock.rented", "出租房"),
                    "户。",
                ),
                heading("1. 入户走访", 3),
                sentence(
                    *report_date_segments("", "，走访出租房"),
                    field(
                        "rental.visit.visits",
                        "走访户数",
                        source="derived",
                        source_key="rental_visit",
                    ),
                    "户，新增",
                    field(
                        "rental.visit.added",
                        "新增",
                        source="derived",
                        source_key="rental_visit",
                    ),
                    "条、变更",
                    field(
                        "rental.visit.changed",
                        "变更",
                        source="derived",
                        source_key="rental_visit",
                    ),
                    "条、注销",
                    field(
                        "rental.visit.cancelled",
                        "注销",
                        source="derived",
                        source_key="rental_visit",
                    ),
                    "条。",
                ),
                sentence(
                    "人均走访",
                    field(
                        "rental.visit.average_visits",
                        "人均走访户数",
                        "decimal",
                        source="derived",
                        source_key="rental_visit",
                    ),
                    "户，人均变动",
                    field(
                        "rental.visit.average_changes",
                        "人均变动数",
                        "decimal",
                        source="derived",
                        source_key="rental_visit",
                    ),
                    "条，户均变动",
                    field(
                        "rental.visit.household_changes",
                        "户均变动数",
                        "decimal",
                        source="derived",
                        source_key="rental_visit",
                    ),
                    "条；完成星级评定",
                    field(
                        "rental.visit.rated",
                        "星级评定数",
                        source="derived",
                        source_key="rental_visit",
                    ),
                    "户，评定率",
                    field(
                        "rental.visit.rating_rate",
                        "星级评定率",
                        "percent",
                        source="derived",
                        source_key="rental_visit",
                    ),
                    "。",
                ),
                table(
                    "rental.visit_table",
                    "出租房屋入户走访统计",
                    [
                        *COMMUNITY_OFFICER,
                        column("grid_member_count", "网格员数"),
                        column("visits", "走访户数"),
                        column("average_visits", "人均走访户数", "decimal"),
                        column("added", "新增"),
                        column("changed", "变更"),
                        column("cancelled", "注销"),
                        column("total_changes", "总变动数"),
                        column("average_changes", "人均变动数", "decimal"),
                        column("household_changes", "户均变动数", "decimal"),
                        column("rated", "星级评定数"),
                        column("rating_rate", "星级评定率", "percent"),
                    ],
                    source="system",
                    source_key="rental_visit",
                    row_mode="system",
                ),
                sentence(*rank_fields(
                    "rental.analysis.average_visits_bottom",
                    "人均走访户数",
                ), style="analysis"),
                sentence(*rank_fields(
                    "rental.analysis.average_changes_bottom",
                    "人均变动数",
                ), style="analysis"),
                sentence(*rank_fields(
                    "rental.analysis.household_changes_bottom",
                    "户均变动数",
                ), style="analysis"),
                heading("2. 倒查质态", 3),
                sentence(
                    *report_date_segments("", "，倒查出租房"),
                    field("rental.reverse.houses", "倒查出租房数"),
                    "户、流动人口",
                    field("rental.reverse.people", "电话核查流口数"),
                    "人；发现同住漏登记",
                    field("rental.reverse.missing_cohabitants", "同住漏登记"),
                    "人、应注销未注销",
                    field("rental.reverse.should_cancel", "应注销未注销"),
                    "人、未星级评定",
                    field("rental.reverse.unrated", "未星级评定"),
                    "户、评定隐患错误",
                    field("rental.reverse.rating_errors", "评定隐患错误"),
                    "户、手机号码错误",
                    field("rental.reverse.phone_errors", "手机号码错误"),
                    "人。",
                    style="analysis",
                ),
                table(
                    "rental.reverse_table",
                    "出租房屋倒查质态",
                    [
                        *COMMUNITY_OFFICER,
                        column("rental_code", "出租房屋编号", "text", width=130),
                        column("people_checked", "电话核查流口数"),
                        column("landlord_inaccurate", "房东信息不准确数"),
                        column("renter_inaccurate", "实际出租人不准确数"),
                        column("phone_errors", "流口手机号码错误数"),
                        column("missing_cohabitants", "同住漏登记人数"),
                        column("should_cancel", "应注销未注销数"),
                        column("unrated", "未星级评定数"),
                        column("rating_errors", "评定隐患错误数"),
                    ],
                    row_mode="community",
                ),
                text_block(
                    "rental.reverse_analysis",
                    "倒查质态问题分析",
                    rows=4,
                ),
                heading("3. 指令抽查", 3),
                table(
                    "rental.spotcheck_table",
                    "指令抽查",
                    [
                        *COMMUNITY_OFFICER,
                        column("inspector", "核查人", "text"),
                        column("instruction", "指令明细", "textarea", width=220),
                        column("reported_result", "反馈结果", "textarea", width=180),
                        column("actual_result", "实际情况", "textarea", width=180),
                    ],
                ),
                text_block(
                    "rental.spotcheck_analysis",
                    "指令抽查问题分析",
                    rows=4,
                ),
                heading("责任落实", 3),
                sentence(
                    field(
                        "meta.month",
                        "月份",
                        source="system",
                        source_key="calendar",
                        width=64,
                    ),
                    "月须完成房东处罚",
                    field("rental.landlord_penalty.target", "处罚任务"),
                    "起，截止",
                    field(
                        "meta.month",
                        "月份",
                        source="system",
                        source_key="calendar",
                        width=64,
                    ),
                    "月",
                    field(
                        "meta.day",
                        "日期",
                        source="system",
                        source_key="calendar",
                        width=64,
                    ),
                    "日，已完成",
                    field("rental.landlord_penalty.completed", "已完成"),
                    "起，",
                    field(
                        "rental.landlord_penalty.note",
                        "处罚任务补充说明",
                        "text",
                        required=False,
                        width=300,
                    ),
                    "。",
                ),
                sentence(
                    "当日涉警房源",
                    field("rental.police_related_houses", "涉警出租房"),
                    "户，",
                    field(
                        "rental.police_related_detail",
                        "涉警房源补充说明",
                        "text",
                        required=False,
                        width=360,
                    ),
                    "。",
                ),
                heading("4. 管理手段", 3, combine_with_next=False),
                sentence(
                    "（1）平安码：",
                    *report_date_segments("", "扫码总数"),
                    field("rental.safe_code.total_scans", "扫码总数"),
                    "次，其中巡逻",
                    field("rental.safe_code.patrol_scans", "巡逻扫码"),
                    "次、办案大厅",
                    field("rental.safe_code.case_hall_scans", "办案大厅扫码"),
                    "次、户籍大厅",
                    field("rental.safe_code.household_hall_scans", "户籍大厅扫码"),
                    "次、社会面",
                    field("rental.safe_code.social_scans", "社会面扫码"),
                    "次；下发指令",
                    field("rental.safe_code.instructions", "下发指令"),
                    "条，预警率",
                    field("rental.safe_code.warning_rate", "预警率", "percent"),
                    "，新增登记",
                    field("rental.safe_code.registrations", "新增登记"),
                    "人，有效扫码率",
                    field("rental.safe_code.valid_rate", "有效扫码率", "percent"),
                    "。",
                    style="strong",
                ),
                sentence(
                    "（2）管家码：累计注册出租户",
                    field("rental.manager_code.registered", "累计注册"),
                    "户，",
                    *report_date_segments("", "新增注册"),
                    field("rental.manager_code.added", "新增注册"),
                    "户，活跃账号",
                    field("rental.manager_code.active", "活跃账号"),
                    "个，扫码",
                    field("rental.manager_code.scans", "扫码次数"),
                    "次，下发指令",
                    field("rental.manager_code.instructions", "指令数"),
                    "条，预警率",
                    field("rental.manager_code.warning_rate", "预警率", "percent"),
                    "。",
                    style="strong",
                ),
            ],
        },
        {
            "id": "self_owned",
            "title": "三、自购自住房屋管理",
            "blocks": [
                heading("1. 走访数", 3),
                table(
                    "self_owned.visit_table",
                    "自购自住房屋走访",
                    [
                        column("grid_member", "网格员", "text", width=110),
                        column("visits", "走访数"),
                        column("changed", "变更"),
                        column("cancelled", "注销"),
                    ],
                    source="system",
                    source_key="self_owned_visit",
                    row_mode="system",
                    help_text="按所选日期读取入户走访中的自购房网格员汇总，仍可人工修改。",
                ),
                heading("2. 抽查房屋问题", 3),
                table(
                    "self_owned.spotcheck_table",
                    "自购自住房屋抽查",
                    [
                        column("grid_member", "网格员", "text", width=110),
                        column("house", "抽查房屋", "text", width=180),
                        column("problem", "问题", "textarea", width=260),
                    ],
                ),
                text_block(
                    "self_owned.analysis",
                    "自购自住房屋问题分析",
                    rows=4,
                ),
            ],
        },
        {
            "id": "priority",
            "title": "四、重点人员管控",
            "blocks": [
                sentence(
                    *report_date_segments("截至", "24时，辖区前科人员"),
                    field("priority.stock.criminal_record", "前科人员"),
                    "人、严重精神障碍患者",
                    field("priority.stock.mental_health", "严重精神障碍患者"),
                    "人，其中流动人口",
                    field("priority.stock.floating", "其中流动人口"),
                    "人，未登记暂住",
                    field("priority.stock.unregistered", "未登记暂住"),
                    "人。",
                ),
                heading("1. 列管、撤管", 3),
                sentence(
                    *report_date_segments("", "，新增列管"),
                    field("priority.management.added", "新增列管"),
                    "人、撤管",
                    field("priority.management.removed", "撤管"),
                    "人；迁出流动人口撤管",
                    field("priority.management.moved_removed", "迁出撤管"),
                    "人，暂住登记未同步注销",
                    field("priority.management.unsynced_cancelled", "未同步注销"),
                    "人。",
                ),
                heading("2. 见面质态", 3),
                sentence(
                    "完成见面",
                    field("priority.meeting.completed", "完成见面"),
                    "人，其中已见面",
                    field("priority.meeting.met", "已见面"),
                    "人、未见面",
                    field("priority.meeting.not_met", "未见面"),
                    "人，发现问题",
                    field("priority.meeting.problems", "问题数"),
                    "个。",
                ),
                heading("3. 动态情况", 3),
                sentence(
                    "重点人员涉警",
                    field("priority.dynamic.key_people", "重点人员涉警人数"),
                    "人",
                    field("priority.dynamic.key_cases", "重点人员涉警警情"),
                    "起；五失人员涉警",
                    field("priority.dynamic.five_loss_people", "五失涉警人数"),
                    "人",
                    field("priority.dynamic.five_loss_cases", "五失涉警警情"),
                    "起；命案防范人员涉警",
                    field("priority.dynamic.homicide_people", "命案防范涉警人数"),
                    "人",
                    field("priority.dynamic.homicide_cases", "命案防范涉警警情"),
                    "起；精神障碍患者涉警",
                    field("priority.dynamic.mental_people", "精神障碍涉警人数"),
                    "人",
                    field("priority.dynamic.mental_cases", "精神障碍涉警警情"),
                    "起。",
                ),
                table(
                    "priority.police_table",
                    "重点人员涉警明细",
                    [
                        *COMMUNITY_OFFICER,
                        column("name", "重点人员姓名", "text"),
                        column("person_type", "人员类别", "text"),
                        column("managed", "是否列管", "text"),
                        column("police_date", "涉警日期", "text", width=130),
                        column("police_number", "涉警编号", "text", width=160),
                        column("content", "警情内容", "textarea", width=280),
                        column("result", "处警结果", "textarea", width=220),
                    ],
                ),
            ],
        },
        {
            "id": "disputes",
            "title": "五、矛盾纠纷化解",
            "blocks": [
                sentence(
                    *report_full_date_segments(
                        "截至",
                        "24时，辖区存量未决矛盾纠纷档案",
                    ),
                    field("disputes.stock.total", "存量未决档案"),
                    "起，其中高度关注",
                    field("disputes.stock.high", "高度关注"),
                    "起、重点关注",
                    field("disputes.stock.key", "重点关注"),
                    "起、一般关注",
                    field("disputes.stock.normal", "一般关注"),
                    "起。",
                ),
                sentence(
                    *report_date_segments("", "，新下发矛盾纠纷"),
                    field("disputes.daily.added", "新下发矛盾纠纷"),
                    "起、新建立未决档案",
                    field("disputes.daily.archived", "新建未决档案"),
                    "起；化解矛盾纠纷",
                    field("disputes.daily.resolved_total", "化解矛盾纠纷"),
                    "起，其中回访化解",
                    field("disputes.daily.revisit_resolved", "回访化解"),
                    "起、工作化解",
                    field("disputes.daily.mediation_resolved", "工作化解"),
                    "起、未决档案化解",
                    field("disputes.daily.archive_resolved", "未决档案化解"),
                    "起，实际组织调解",
                    field("disputes.daily.mediations", "组织调解"),
                    "起。",
                    style="analysis",
                ),
                table(
                    "disputes.table",
                    "矛盾纠纷化解情况",
                    [
                        *COMMUNITY_OFFICER,
                        group("回访阶段", [
                            column("issued_today", "当日下发纠纷数"),
                            column("pending_revisit", "3日内待回访数"),
                            column("revisit_resolved", "回访化解数"),
                        ]),
                        group("调解阶段", [
                            column("pending_mediation", "7日内待调解数"),
                            column("mediation_resolved", "工作化解数"),
                        ]),
                        group("未决档案", [
                            column("stock_2025", "2025年存量"),
                            column("stock_2026", "2026年存量"),
                            column("high_attention", "高度关注"),
                            column("key_attention", "重点关注"),
                            column("normal_attention", "一般关注"),
                            column("archived_today", "当日建档数"),
                            column("resolved_today", "当日化解数"),
                        ]),
                        group("组织调解", [
                            column("mediation_count", "实际组织调解数"),
                            column(
                                "mediation_note",
                                "调解情况说明",
                                "textarea",
                                width=220,
                            ),
                        ]),
                    ],
                    row_mode="community",
                ),
            ],
        },
        {
            "id": "fire",
            "title": "六、消防安全监管",
            "blocks": [
                sentence(
                    *report_date_segments("", "，检查单位"),
                    field("fire.daily.checked", "检查单位数"),
                    "家，发现隐患",
                    field("fire.daily.hazards", "发现隐患数"),
                    "处，现场整改",
                    field("fire.daily.rectified", "现场整改数"),
                    "处，消防处罚",
                    field("fire.daily.penalties", "处罚数"),
                    "起，查封",
                    field("fire.daily.sealed", "查封数"),
                    "家。",
                ),
                table(
                    "fire.table",
                    "消防安全检查情况",
                    [
                        *COMMUNITY_OFFICER,
                        column("task_count", "常规检查任务数"),
                        column("checked_count", "已检查数"),
                        column("completion_rate", "检查完成率", "percent"),
                        column("daily_units", "当日检查单位数"),
                        column("hazards", "发现隐患数"),
                        column("rectified", "现场整改数"),
                        column("major_hazards", "重大隐患单位数"),
                        column("penalties", "累计消防处罚数"),
                        column("sealed", "累计消防查封数"),
                        column("note", "备注", "textarea", width=180, required=False),
                    ],
                    row_mode="community",
                ),
            ],
        },
        {
            "id": "security",
            "title": "七、治安要素监管",
            "blocks": [
                heading("1. 行业场所", 3),
                sentence(
                    *report_date_segments("", "，检查行业场所"),
                    field("security.venues.checked", "检查场所数"),
                    "家，计分",
                    field("security.venues.scored", "计分场所数"),
                    "家，关停",
                    field("security.venues.closed", "关停场所数"),
                    "家，处罚",
                    field("security.venues.penalized", "处罚场所数"),
                    "家。",
                ),
                table(
                    "security.venues_table",
                    "行业场所管理",
                    [
                        column("venue_type", "场所类别", "text", width=110),
                        group("实地检查概况", [
                            column("checked", "检查场所数"),
                            column("undeclared_staff", "从业人员未申报数"),
                            column("unregistered_staff", "从业人员未登记数"),
                            column("security_hazards", "治安隐患数"),
                            column("fire_hazards", "消防隐患数"),
                        ]),
                        group("视频巡查概况", [
                            column("video_checked", "巡查场所数"),
                            column("camera_abnormal", "监控异常场所数"),
                            column("clues", "问题线索数"),
                        ]),
                        group("警情发案概况", [
                            column("police_cases", "发生警情数"),
                            column("police_type", "警情类别", "text"),
                            column("police_number", "警情编号", "text", width=150),
                        ]),
                        group("处置措施", [
                            column("scored", "计分场所数"),
                            column("closed", "关停场所数"),
                            column("penalized", "场所处罚数"),
                            column("note", "备注说明", "textarea", width=180, required=False),
                        ]),
                    ],
                    row_mode="fixed",
                    fixed_rows=[
                        {"venue_type": "足浴"},
                        {"venue_type": "浴室"},
                        {"venue_type": "酒吧/KTV"},
                        {"venue_type": "宾馆"},
                        {"venue_type": "网约房/民宿"},
                        {"venue_type": "其他单位"},
                    ],
                ),
                text_block(
                    "security.venue_hazard_details",
                    "检查隐患明细",
                    rows=5,
                ),
                heading("2. 犬只管理", 3),
                sentence(
                    *report_date_segments("", "，犬只处罚"),
                    field("security.dog.penalties", "犬只处罚数"),
                    "起，收容犬只",
                    field("security.dog.impounded", "收容犬只数"),
                    "只，犬类警情",
                    field("security.dog.police_cases", "犬类警情数"),
                    "起。",
                ),
                table(
                    "security.dog_table",
                    "犬只管理任务",
                    [
                        *COMMUNITY_OFFICER,
                        column("monthly_target", "本月处罚任务"),
                        column("completed", "已完成"),
                    ],
                    row_mode="community",
                ),
                heading("3. 涉黄涉赌警情", 3),
                sentence(
                    *report_date_segments("", "，涉黄警情"),
                    field("security.yellow_gamble.yellow_cases", "涉黄警情"),
                    "起、涉赌警情",
                    field("security.yellow_gamble.gamble_cases", "涉赌警情"),
                    "起，未完成回访涉黄",
                    field("security.yellow_gamble.yellow_pending", "涉黄待回访"),
                    "起、涉赌",
                    field("security.yellow_gamble.gamble_pending", "涉赌待回访"),
                    "起。",
                ),
                table(
                    "security.yellow_gamble_table",
                    "涉黄涉赌警情明细",
                    [
                        column("police_number", "警情编号", "text", width=160),
                        column("content", "报警内容", "textarea", width=260),
                        column("location", "报警地点", "text", width=180),
                        column("phone", "报警电话", "text", width=130),
                        column("repeat_count", "重复次数"),
                        column("officer", "责任民警", "text", width=100),
                    ],
                ),
                text_block(
                    "security.yellow_gamble_analysis",
                    "涉黄涉赌问题分析",
                    rows=4,
                ),
            ],
        },
        {
            "id": "fraud",
            "title": "八、电诈发案劝阻",
            "blocks": [
                heading("1. 电诈发案", 3),
                sentence(
                    *report_date_segments("", "，电诈案件"),
                    field("fraud.cases.daily", "当日案件数"),
                    "起，年内累计",
                    field("fraud.cases.year_total", "年内累计"),
                    "起；与",
                    field("fraud.cases.compare_year", "比较年份", "text"),
                    "年同期",
                    field("fraud.cases.compare_total", "同期案件数"),
                    "起相比，下降",
                    field("fraud.cases.decline_rate", "下降率", "percent"),
                    "。",
                ),
                heading("2. 精准预警", 3),
                sentence(
                    *report_date_segments("", "，预警"),
                    field("fraud.warning.total", "预警总数"),
                    "条，见面",
                    field("fraud.warning.met", "见面数"),
                    "人，流转",
                    field("fraud.warning.transferred", "流转数"),
                    "人，次日跟进",
                    field("fraud.warning.next_day", "次日跟进数"),
                    "人，人在苏州外",
                    field("fraud.warning.outside_suzhou", "苏州外人数"),
                    "人，见面率",
                    field("fraud.warning.meeting_rate", "见面率", "percent"),
                    "；唤醒",
                    field("fraud.warning.awakened", "唤醒数"),
                    "人，唤醒率",
                    field("fraud.warning.awaken_rate", "唤醒率", "percent"),
                    "，删除涉诈信息",
                    field("fraud.warning.deleted_info", "删除涉诈信息"),
                    "条，阻断涉诈要素",
                    field("fraud.warning.blocked_elements", "阻断要素"),
                    "个。",
                ),
                text_block(
                    "fraud.large_amount_case",
                    "大额资金防阻案例",
                    rows=5,
                ),
                sentence(
                    "见面核查中发现问题",
                    field("fraud.meeting_problem.total", "问题总数"),
                    "个，其中信息不准确",
                    field("fraud.meeting_problem.inaccurate", "信息不准确"),
                    "个、处置不规范",
                    field("fraud.meeting_problem.improper", "处置不规范"),
                    "个。",
                ),
            ],
        },
        {
            "id": "notices",
            "title": "九、通知通报",
            "blocks": [
                heading("一、三单一报进度", 3),
                table(
                    "notices.table",
                    "三单一报进度",
                    [
                        column("sequence", "序号", "text", width=66),
                        column("title", "通知标题名称", "text", width=240),
                        column("line", "条线", "text", width=90),
                        column("task_count", "任务数量"),
                        column("issued_at", "下发时间", "text", width=140),
                        column("owner", "责任人", "text", width=90),
                        column("deadline", "任务截至时间", "text", width=130),
                        column("progress", "当前进度", "textarea", width=180),
                    ],
                ),
                text_block(
                    "notices.analysis",
                    "通知通报补充说明",
                    rows=4,
                ),
            ],
        },
        {
            "id": "special",
            "title": "十、专项工作",
            "blocks": [
                heading("叮咛行动", 3, combine_with_next=False),
                sentence(
                    "第三批次任务总数",
                    field("special.dingning.batch_total", "第三批次任务总数"),
                    "人，已核查未见面",
                    field(
                        "special.dingning.checked_not_met",
                        "已核查未见面数",
                    ),
                    "人，下发",
                    field(
                        "special.dingning.issued_pending",
                        "下发待核查见面数",
                    ),
                    "人待核查见面，已核查",
                    field("special.dingning.checked", "已核查人数"),
                    "人，见面宣导",
                    field("special.dingning.promoted", "见面宣导数"),
                    "人（暂不离吴",
                    field("special.dingning.not_returning", "暂不离吴数"),
                    "人，拒绝配合等其他特殊情况",
                    field("special.dingning.other", "其他特殊情况数"),
                    "人），其中社区警务队宣传完成率为",
                    field(
                        "special.dingning.community_completion_rate",
                        "社区警务队宣传完成率",
                        "percent",
                        precision=2,
                    ),
                    "，综合指挥室+辅警办公室宣传完成率为",
                    field(
                        "special.dingning.command_completion_rate",
                        "综合指挥室和辅警办公室宣传完成率",
                        "percent",
                        precision=2,
                    ),
                    "。",
                ),
                table(
                    "special.dingning_table",
                    "叮咛行动进度",
                    [
                        column("department", "部门", "text", width=150),
                        column("pending", "待核查数"),
                        column("checked", "已核查人数"),
                        column("valid", "有效核查数"),
                        column("promoted", "见面宣导数"),
                        column("unneeded", "无需宣传数"),
                        column("not_returning", "暂不离吴数"),
                        column("other", "其他特殊情况数"),
                        column("valid_rate", "有效核查率", "percent"),
                    ],
                    row_mode="fixed",
                    fixed_rows=[
                        {"department": "社区警务队"},
                        {"department": "执法办案队"},
                        {"department": "综合指挥室、辅警办公室"},
                    ],
                ),
                heading("监控推广", 3),
                table(
                    "special.monitor_table",
                    "监控推广进度",
                    [
                        column("sequence", "序号", "text", width=62),
                        column("community_officer", "社区民警", "text", width=100),
                        column("total_target", "监控总攻坚数"),
                        group("1200人像", [
                            column("face_target", "攻坚任务数"),
                            column("face_completed", "完成数"),
                            column("face_rate", "完成率", "percent"),
                        ]),
                        group("360监控", [
                            column("camera_target", "监控任务数"),
                            column("camera_completed", "完成数"),
                            column("camera_rate", "完成率", "percent"),
                        ]),
                    ],
                ),
            ],
        },
    ],
}


def get_schema() -> dict:
    return deepcopy(SCHEMA)


def leaf_columns(columns: Iterable[dict]) -> list[dict]:
    result: list[dict] = []
    for item in columns:
        children = item.get("children")
        if children:
            result.extend(leaf_columns(children))
        else:
            result.append(item)
    return result


def field_definitions() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for section in SCHEMA["sections"]:
        for block in section["blocks"]:
            if block["type"] == "sentence":
                for segment in block["segments"]:
                    if isinstance(segment, dict) and segment.get("id"):
                        result[segment["id"]] = segment
            elif block["type"] in {"textarea", "table"}:
                definition = block["field"]
                result[definition["id"]] = definition
    return result


def _empty_row(columns: list[dict]) -> dict:
    return {item["key"]: "" for item in leaf_columns(columns)}


def default_manual_values(
    communities: list[str] | None = None,
    community_officers: dict[str, str] | None = None,
    community_grid_member_counts: dict[str, int] | None = None,
) -> dict:
    communities = communities or []
    community_officers = community_officers or {}
    community_grid_member_counts = community_grid_member_counts or {}
    result: dict[str, Any] = {}
    for field_id, definition in field_definitions().items():
        if definition["source"] != "manual":
            continue
        if definition["type"] != "table":
            result[field_id] = None
            continue
        columns = definition["columns"]
        mode = definition.get("row_mode", "detail")
        if mode == "community":
            community_key = definition.get(
                "community_key",
                "responsibility_area",
            )
            column_keys = {
                item["key"]
                for item in leaf_columns(columns)
            }
            rows = []
            for community in communities:
                row = {
                    **_empty_row(columns),
                    community_key: community,
                }
                if "community_officer" in column_keys:
                    row["community_officer"] = community_officers.get(
                        community,
                        "",
                    )
                if (
                    "grid_member_count" in column_keys
                    and community in community_grid_member_counts
                ):
                    row["grid_member_count"] = (
                        community_grid_member_counts[community]
                    )
                rows.append(row)
            result[field_id] = rows or [_empty_row(columns)]
        elif mode == "fixed":
            result[field_id] = [
                {**_empty_row(columns), **row}
                for row in definition.get("fixed_rows", [])
            ]
        else:
            result[field_id] = [_empty_row(columns)]
    return result


def fill_community_grid_member_counts(
    values: dict | None,
    community_grid_member_counts: dict[str, int] | None,
) -> dict:
    """只补齐社区表中尚未填写的网格员数，不覆盖人工修改。"""
    result = deepcopy(values) if isinstance(values, dict) else {}
    counts = community_grid_member_counts or {}
    if not counts:
        return result
    for field_id, definition in field_definitions().items():
        if (
            definition["source"] != "manual"
            or definition["type"] != "table"
            or definition.get("row_mode") != "community"
        ):
            continue
        column_keys = {
            item["key"]
            for item in leaf_columns(definition["columns"])
        }
        if "grid_member_count" not in column_keys:
            continue
        community_key = definition.get(
            "community_key",
            "responsibility_area",
        )
        rows = result.get(field_id)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            community = str(row.get(community_key) or "").strip()
            if (
                community in counts
                and row.get("grid_member_count") in (None, "")
            ):
                row["grid_member_count"] = counts[community]
    return result


def _cell_value(value: Any, column_definition: dict) -> Any:
    if value is None or value == "":
        return ""
    field_type = column_definition.get("type", "text")
    if field_type in {"number", "decimal", "percent"}:
        if isinstance(value, bool):
            return ""
        try:
            return float(value)
        except (TypeError, ValueError):
            return ""
    return str(value)[:2000]


def sanitize_values(values: dict | None, *, source: str) -> dict:
    """只保存模板允许的字段，避免草稿写入任意 JSON。"""
    values = values if isinstance(values, dict) else {}
    definitions = field_definitions()
    result: dict[str, Any] = {}
    accepted_sources = {"system", "derived"} if source == "system" else {source}
    for field_id, value in values.items():
        definition = definitions.get(field_id)
        if not definition or definition["source"] not in accepted_sources:
            continue
        if definition["type"] == "table":
            if not isinstance(value, list):
                continue
            columns = {
                item["key"]: item
                for item in leaf_columns(definition["columns"])
            }
            result[field_id] = [
                {
                    key: _cell_value(row.get(key), column_definition)
                    for key, column_definition in columns.items()
                }
                for row in value[:200]
                if isinstance(row, dict)
            ]
        elif value is None or value == "":
            result[field_id] = None
        elif definition["type"] in {"number", "decimal", "percent"}:
            if isinstance(value, bool):
                continue
            try:
                result[field_id] = float(value)
            except (TypeError, ValueError):
                continue
        else:
            result[field_id] = str(value)[:10000]
    return result


def _number(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(0)


def _sum(rows: list[dict], key: str) -> Decimal:
    return sum((_number(row.get(key)) for row in rows), Decimal(0))


def _rounded(value: Decimal, places: str = "0.1") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _ratio(numerator: Decimal, denominator: Decimal) -> float | None:
    if denominator <= 0:
        return None
    return _rounded(numerator * Decimal(100) / denominator)


def derive_values(values: dict[str, Any]) -> dict[str, Any]:
    """从两张可编辑系统表计算概览；人工覆盖值在调用方最后合并。"""
    derived: dict[str, Any] = {}
    instruction = values.get("flow.instruction_table")
    if isinstance(instruction, list) and instruction:
        rows = [row for row in instruction if isinstance(row, dict)]
        total = _sum(rows, "total")
        unchecked = _sum(rows, "unchecked")
        checked = _sum(rows, "checked")
        unable = _sum(rows, "unable")
        members = _sum(rows, "grid_member_count")
        derived.update({
            "flow.instruction.total": float(total),
            "flow.instruction.unchecked": float(unchecked),
            "flow.instruction.checked": float(checked),
            "flow.instruction.unable": float(unable),
            "flow.instruction.completion_rate": _ratio(checked, total),
            "flow.instruction.ground_rate": _ratio(
                max(checked - unable, Decimal(0)),
                checked,
            ),
            "flow.instruction.average_checked": (
                _rounded(checked / members)
                if members > 0
                else None
            ),
        })

    rental = values.get("rental.visit_table")
    if isinstance(rental, list) and rental:
        rows = [row for row in rental if isinstance(row, dict)]
        visits = _sum(rows, "visits")
        added = _sum(rows, "added")
        changed = _sum(rows, "changed")
        cancelled = _sum(rows, "cancelled")
        members = _sum(rows, "grid_member_count")
        rated = _sum(rows, "rated")
        total_changes = added + changed + cancelled
        derived.update({
            "rental.visit.visits": float(visits),
            "rental.visit.added": float(added),
            "rental.visit.changed": float(changed),
            "rental.visit.cancelled": float(cancelled),
            "rental.visit.average_visits": (
                _rounded(visits / members)
                if members > 0
                else None
            ),
            "rental.visit.average_changes": (
                _rounded(total_changes / members)
                if members > 0
                else None
            ),
            "rental.visit.household_changes": (
                _rounded(total_changes / visits)
                if visits > 0
                else None
            ),
            "rental.visit.rated": float(rated),
            "rental.visit.rating_rate": _ratio(rated, visits),
        })
    return derived


def effective_values(draft: dict) -> dict[str, Any]:
    snapshot = draft.get("system_snapshot") or {}
    system_values = dict(snapshot.get("values") or {})
    if system_values.get("meta.year") in (None, ""):
        business_date = str(
            snapshot.get("business_date")
            or draft.get("business_date")
            or ""
        )
        try:
            system_values["meta.year"] = int(business_date[:4])
        except (TypeError, ValueError):
            pass
    manual_values = draft.get("manual_values") or {}
    overrides = draft.get("override_values") or {}
    table_inputs = {**system_values, **manual_values, **overrides}
    return {
        **system_values,
        **manual_values,
        **derive_values(table_inputs),
        **overrides,
    }
