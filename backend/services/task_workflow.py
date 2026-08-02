"""流口岗手机任务工作台的业务字段和状态规则。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskWorkflow:
    parser_type: str
    label: str
    result_field: str
    phone_fields: tuple[str, ...]
    title_fields: tuple[str, ...]
    address_fields: tuple[str, ...]
    date_fields: tuple[str, ...]
    secondary_fields: tuple[str, ...] = ()
    valid_results: tuple[str, ...] = ()

    def state(self, values: dict[str, str]) -> str:
        """返回 unchecked、checked 或 completed。"""
        result = str(values.get(self.result_field, "") or "").strip()
        if self.valid_results:
            return "completed" if result in self.valid_results else "unchecked"
        if result:
            return "completed"
        if any(str(values.get(field, "") or "").strip() for field in ("现住址",)):
            return "checked"
        return "unchecked"

    def needs_review(
        self,
        values: dict[str, str],
        *,
        source_count: int = 1,
        conflict: bool = False,
    ) -> bool:
        if conflict or source_count > 1:
            return True
        result = str(values.get(self.result_field, "") or "").strip()
        if "无法核实" not in result or not self.secondary_fields:
            return False
        return not any(
            str(values.get(field, "") or "").strip()
            for field in self.secondary_fields
        )

    def first_value(self, values: dict[str, str], fields: tuple[str, ...]) -> str:
        for field in fields:
            value = str(values.get(field, "") or "").strip()
            if value:
                return value
        return ""

    def summary(self, values: dict[str, str]) -> dict[str, str]:
        return {
            "title": self.first_value(values, self.title_fields) or "未填写姓名",
            "phone": self.first_value(values, self.phone_fields),
            "address": self.first_value(values, self.address_fields),
            "date": self.first_value(values, self.date_fields),
            "result": str(values.get(self.result_field, "") or "").strip(),
        }


TASK_WORKFLOWS: dict[str, TaskWorkflow] = {
    "全链条": TaskWorkflow(
        parser_type="全链条",
        label="全链条",
        result_field="核查结果",
        phone_fields=("电话号码",),
        title_fields=("姓名",),
        address_fields=("现住址", "地址"),
        date_fields=("截止日期", "下发日期"),
        secondary_fields=("二次反馈",),
    ),
    "出租房屋核查": TaskWorkflow(
        parser_type="出租房屋核查",
        label="出租房屋核查",
        result_field="核查结果",
        phone_fields=("手机号码",),
        title_fields=("姓名",),
        address_fields=("现住址", "房屋地址"),
        date_fields=("截止时间", "下发时间"),
        secondary_fields=("二次反馈",),
    ),
    "寄递业": TaskWorkflow(
        parser_type="寄递业",
        label="寄递业",
        result_field="核查结果",
        phone_fields=("手机号码",),
        title_fields=("姓名", "参考姓名"),
        address_fields=("现住址", "地址1"),
        date_fields=("截止时间", "下发时间"),
        secondary_fields=("二次反馈",),
    ),
    "疑似未注销模型三": TaskWorkflow(
        parser_type="疑似未注销模型三",
        label="疑似未注销模型三",
        result_field="核查结果",
        phone_fields=("联系方式",),
        title_fields=("姓名",),
        address_fields=("地址",),
        date_fields=("截止时间",),
        valid_results=("近期反吴", "在吴", "离吴"),
    ),
    "疑似返苏": TaskWorkflow(
        parser_type="疑似返苏",
        label="疑似返苏",
        result_field="核查反馈",
        phone_fields=("联系号码",),
        title_fields=("姓名",),
        address_fields=("现住址", "高频抓拍小区"),
        date_fields=("截止日期", "下发日期"),
        secondary_fields=("二次核查结果",),
    ),
}

MOBILE_TASK_TYPES = tuple(TASK_WORKFLOWS)


def task_state(parser_type: str, values: dict[str, str]) -> str:
    workflow = TASK_WORKFLOWS.get(parser_type)
    return workflow.state(values) if workflow else ""
