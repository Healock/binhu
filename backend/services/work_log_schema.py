"""工作日志日报的稳定字段结构。

字段 ID 会写入草稿，后续模板调整时不能随意改名。
"""

from copy import deepcopy


TEMPLATE_VERSION = "daily-v1"
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
    help_text: str = "",
    columns: list[dict] | None = None,
) -> dict:
    result = {
        "id": field_id,
        "label": label,
        "type": field_type,
        "source": source,
        "required": required,
    }
    if help_text:
        result["help"] = help_text
    if columns:
        result["columns"] = columns
    return result


TABLE_DETAIL_COLUMNS = [
    {"key": "community", "label": "社区"},
    {"key": "name", "label": "姓名"},
    {"key": "content", "label": "情况说明"},
    {"key": "result", "label": "处置结果"},
]

SCHEMA = {
    "template_version": TEMPLATE_VERSION,
    "report_types": REPORT_TYPES,
    "sections": [
        {
            "id": "basic",
            "title": "基础数据",
            "description": "填写人口、登记和注销等基础情况。",
            "fields": [
                field("basic.total_population", "实有人口总数"),
                field("basic.registered_population", "户籍人口"),
                field("basic.floating_population", "流动人口"),
                field("basic.flow_added", "当日新增登记"),
                field("basic.active_cancelled", "主动注销"),
                field("basic.passive_cancelled", "被动注销"),
                field(
                    "basic.analysis",
                    "基础数据情况说明",
                    "textarea",
                    required=False,
                ),
            ],
        },
        {
            "id": "rental",
            "title": "出租房",
            "description": "走访指标由系统按所选业务日期读取，可以人工覆盖。",
            "fields": [
                field("rental.visits", "走访户数", source="system"),
                field("rental.added", "新增", source="system"),
                field("rental.changed", "变更", source="system"),
                field("rental.cancelled", "注销", source="system"),
                field("rental.total_changes", "总变动数", source="system"),
                field("rental.person_avg_visits", "人均走访户数", "decimal", source="system"),
                field("rental.person_avg_changes", "人均变动数", "decimal", source="system"),
                field("rental.household_avg_changes", "户均变动数", "decimal", source="system"),
                field("rental.rated", "星级评定数", source="system"),
                field("rental.rating_rate", "星级评定率", "percent", source="system"),
                field("rental.ranking", "社区排名", "textarea", source="system"),
                field("rental.current_stock", "出租房当前底数"),
                field("rental.reverse_checks", "反向核查数"),
                field("rental.analysis", "出租房工作说明", "textarea", required=False),
            ],
        },
        {
            "id": "self_owned",
            "title": "自购房",
            "description": "自购房岗位的走访指标由系统填写。",
            "fields": [
                field("self_owned.visits", "走访户数", source="system"),
                field("self_owned.added", "新增", source="system"),
                field("self_owned.changed", "变更", source="system"),
                field("self_owned.cancelled", "注销", source="system"),
                field("self_owned.total_changes", "总变动数", source="system"),
                field("self_owned.person_avg_visits", "人均走访户数", "decimal", source="system"),
                field("self_owned.person_avg_changes", "人均变动数", "decimal", source="system"),
                field("self_owned.household_avg_changes", "户均变动数", "decimal", source="system"),
                field("self_owned.rated", "星级评定数", source="system"),
                field("self_owned.rating_rate", "星级评定率", "percent", source="system"),
                field("self_owned.ranking", "社区排名", "textarea", source="system"),
                field("self_owned.analysis", "自购房工作说明", "textarea", required=False),
            ],
        },
        {
            "id": "priority",
            "title": "重点人员",
            "description": "模型三采用固定两列口径，不受账号个性化设置影响。",
            "fields": [
                field("priority.model3_total", "模型三任务总数", source="system"),
                field("priority.model3_unchecked", "模型三未核查", source="system"),
                field("priority.model3_checked", "模型三已核查", source="system"),
                field("priority.model3_completion_rate", "模型三完成率", "percent", source="system"),
                field("priority.model3_unable", "模型三无法见底数", source="system"),
                field("priority.model3_ground_rate", "模型三见底率", "percent", source="system"),
                field("priority.model3_ranking", "模型三社区排名", "textarea", source="system"),
                field("priority.added", "新增重点人员"),
                field("priority.removed", "解除重点人员"),
                field(
                    "priority.details",
                    "重点人员情况",
                    "table",
                    required=False,
                    columns=TABLE_DETAIL_COLUMNS,
                ),
            ],
        },
        {
            "id": "disputes",
            "title": "矛盾纠纷",
            "fields": [
                field("disputes.stock", "矛盾纠纷存量"),
                field("disputes.added", "当日新增"),
                field("disputes.resolved", "当日化解"),
                field("disputes.unresolved", "未化解"),
                field(
                    "disputes.details",
                    "矛盾纠纷明细",
                    "table",
                    required=False,
                    columns=TABLE_DETAIL_COLUMNS,
                ),
            ],
        },
        {
            "id": "fire",
            "title": "消防",
            "fields": [
                field("fire.checked", "检查单位数"),
                field("fire.hazards", "发现隐患数"),
                field("fire.rectified", "整改隐患数"),
                field(
                    "fire.details",
                    "消防检查明细",
                    "table",
                    required=False,
                    columns=TABLE_DETAIL_COLUMNS,
                ),
            ],
        },
        {
            "id": "security",
            "title": "治安",
            "fields": [
                field("security.venues_checked", "行业场所检查数"),
                field("security.hazards", "治安隐患数"),
                field("security.dogs", "犬只管理数"),
                field("security.special_cases", "黄赌整治数"),
                field("security.analysis", "治安工作说明", "textarea", required=False),
                field(
                    "security.details",
                    "治安检查明细",
                    "table",
                    required=False,
                    columns=TABLE_DETAIL_COLUMNS,
                ),
            ],
        },
        {
            "id": "fraud",
            "title": "电诈",
            "fields": [
                field("fraud.cases", "电诈警情数"),
                field("fraud.warnings", "精准预警数"),
                field("fraud.completed", "预警处置数"),
                field("fraud.analysis", "电诈情况说明", "textarea", required=False),
            ],
        },
        {
            "id": "notices",
            "title": "通知通报",
            "fields": [
                field(
                    "notices.items",
                    "通知通报",
                    "table",
                    required=False,
                    columns=[
                        {"key": "department", "label": "部门"},
                        {"key": "content", "label": "通知内容"},
                        {"key": "deadline", "label": "完成时限"},
                        {"key": "status", "label": "完成情况"},
                    ],
                ),
            ],
        },
        {
            "id": "special",
            "title": "专项工作",
            "fields": [
                field("special.summary", "专项工作说明", "textarea", required=False),
                field(
                    "special.items",
                    "专项工作明细",
                    "table",
                    required=False,
                    columns=TABLE_DETAIL_COLUMNS,
                ),
            ],
        },
    ],
}


def get_schema() -> dict:
    return deepcopy(SCHEMA)


def field_definitions() -> dict[str, dict]:
    return {
        item["id"]: item
        for section in SCHEMA["sections"]
        for item in section["fields"]
    }


def default_manual_values() -> dict:
    return {
        field_id: ([] if definition["type"] == "table" else None)
        for field_id, definition in field_definitions().items()
        if definition["source"] == "manual"
    }


def sanitize_values(values: dict | None, *, source: str) -> dict:
    """只保留字段结构中允许的键，避免草稿写入未知内容。"""
    values = values if isinstance(values, dict) else {}
    definitions = field_definitions()
    result: dict = {}
    for field_id, value in values.items():
        definition = definitions.get(field_id)
        if not definition or definition["source"] != source:
            continue
        if definition["type"] == "table":
            if not isinstance(value, list):
                continue
            allowed = {column["key"] for column in definition["columns"]}
            result[field_id] = [
                {
                    key: str(row.get(key, ""))[:2000]
                    for key in allowed
                }
                for row in value[:200]
                if isinstance(row, dict)
            ]
        elif value is None:
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
