"""疑似返苏 - 在线表格解析器"""
from .base import BaseParser


class SuspectReturnParser(BaseParser):
    parser_type = "疑似返苏"
    table_name = "t_suspect_return"
    COLUMNS = [
        "下发日期", "截止日期", "核查人", "社区", "姓名",
        "身份证号码", "联系号码", "高频抓拍小区", "现住址", "核查反馈",
        "研判", "二次核查结果",
    ]
    DATABASE_COLUMN_ALIASES = {
        "身份证号码": ("身份证号",),
        "二次核查结果": ("二次反馈",),
    }

    def get_business_key(self) -> list[str]:
        return ["身份证号码", "联系号码"]

    def merge_duplicate_row(
        self,
        previous: dict,
        incoming: dict,
    ) -> dict | None:
        """合并同一返苏对象的重复来源行。

        “无需登记”和“无需登记，原因写备注”是同一业务结果的简写与
        完整写法，优先保留较长的写法，避免投影被标记为来源冲突。其他
        不同的非空字段仍然保持冲突，防止把真实不同结果静默覆盖。
        """
        merged = dict(previous)
        result_field = "核查反馈"
        no_registration_results = {"无需登记", "无需登记，原因写备注"}
        for column in self.COLUMNS:
            old_value = str(previous.get(column, "") or "").strip()
            new_value = str(incoming.get(column, "") or "").strip()
            if not old_value:
                merged[column] = new_value
                continue
            if not new_value or old_value == new_value:
                continue
            if (
                column == result_field
                and old_value in no_registration_results
                and new_value in no_registration_results
            ):
                merged[column] = max((old_value, new_value), key=len)
                continue
            return None
        return merged
