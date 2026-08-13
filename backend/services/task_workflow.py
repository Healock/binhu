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
    identity_fields: tuple[str, ...]
    source_fields: tuple[str, ...] = ()
    secondary_fields: tuple[str, ...] = ()
    analysis_fields: tuple[str, ...] = ("研判",)
    valid_results: tuple[str, ...] = ()
    result_options: tuple[str, ...] = ()

    def state(self, values: dict[str, str]) -> str:
        """返回 unchecked、checked 或 completed。"""
        result = str(values.get(self.result_field, "") or "").strip()
        if self.valid_results:
            return "completed" if result in self.valid_results else "unchecked"
        if "无法核实" in result:
            return "checked"
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
        if self.valid_results:
            return False
        result = str(values.get(self.result_field, "") or "").strip()
        return "无法核实" in result

    def review_stage(self, values: dict[str, str]) -> str:
        """无法核实任务按研判是否填写区分复核阶段。"""
        result = str(values.get(self.result_field, "") or "").strip()
        if self.valid_results or "无法核实" not in result:
            return ""
        return "analyzed" if any(
            str(values.get(field, "") or "").strip()
            for field in self.analysis_fields
        ) else "waiting_analysis"

    def first_value(self, values: dict[str, str], fields: tuple[str, ...]) -> str:
        for field in fields:
            value = str(values.get(field, "") or "").strip()
            if value:
                return value
        return ""

    def summary(self, values: dict[str, str]) -> dict[str, str]:
        current_address = (
            str(values.get("现住址", "") or "").strip()
            if "现住址" in self.address_fields
            else ""
        )
        original_address = self.first_value(
            values,
            tuple(field for field in self.address_fields if field != "现住址"),
        )
        return {
            "title": self.first_value(values, self.title_fields) or "未填写姓名",
            "identity_number": self.first_value(values, self.identity_fields),
            "phone": self.first_value(values, self.phone_fields),
            "source": self.first_value(values, self.source_fields),
            "address": current_address or original_address,
            "current_address": current_address,
            "original_address": original_address,
            "deadline": self.first_value(values, self.date_fields[:1]),
            "date": self.first_value(values, self.date_fields),
            "result": str(values.get(self.result_field, "") or "").strip(),
            "analysis": self.first_value(values, self.analysis_fields),
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
        identity_fields=("身份证号",),
        source_fields=("来源",),
        secondary_fields=("二次反馈",),
        result_options=("已登记", "待登记", "无法核实", "移交", "无需登记", "离苏"),
    ),
    "出租房屋核查": TaskWorkflow(
        parser_type="出租房屋核查",
        label="出租房屋核查",
        result_field="核查结果",
        phone_fields=("手机号码",),
        title_fields=("姓名",),
        address_fields=("现住址", "房屋地址"),
        date_fields=("截止时间", "下发时间"),
        identity_fields=("身份证号",),
        secondary_fields=("二次反馈",),
        result_options=(
            "已登记",
            "离苏",
            "常口",
            "无需登记，原因写备注",
            "移交，移交哪个社区写备注",
            "无法核实",
        ),
    ),
    "寄递业": TaskWorkflow(
        parser_type="寄递业",
        label="寄递业",
        result_field="核查结果",
        phone_fields=("手机号码",),
        title_fields=("姓名", "参考姓名"),
        address_fields=("现住址", "地址1"),
        date_fields=("截止时间", "下发时间"),
        identity_fields=("身份证号", "参考身份证号码"),
        secondary_fields=("二次反馈",),
        result_options=(
            "已登记",
            "离苏",
            "无需登记，原因后面备注好",
            "移交，后面移交哪个社区备注好",
            "身份错误",
            "无法核实",
        ),
    ),
    "疑似未注销模型三": TaskWorkflow(
        parser_type="疑似未注销模型三",
        label="疑似未注销模型三",
        result_field="核查结果",
        phone_fields=("联系方式",),
        title_fields=("姓名",),
        address_fields=("地址",),
        date_fields=("截止时间",),
        identity_fields=("身份证号",),
        # “近期反吴”是旧版本曾经写入的错拼值，只用于兼容历史数据。
        valid_results=("近期返吴", "近期反吴", "在吴", "离吴"),
        result_options=("近期返吴", "离吴", "在吴"),
    ),
    "疑似返苏": TaskWorkflow(
        parser_type="疑似返苏",
        label="疑似返苏",
        result_field="核查反馈",
        phone_fields=("联系号码",),
        title_fields=("姓名",),
        address_fields=("现住址", "高频抓拍小区"),
        date_fields=("截止日期", "下发日期"),
        identity_fields=("身份证号码",),
        secondary_fields=("二次核查结果",),
        result_options=("已登记", "无需登记", "移交", "无法核实"),
    ),
}

MOBILE_TASK_TYPES = tuple(TASK_WORKFLOWS)


def task_state(parser_type: str, values: dict[str, str]) -> str:
    workflow = TASK_WORKFLOWS.get(parser_type)
    return workflow.state(values) if workflow else ""
