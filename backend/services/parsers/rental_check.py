"""出租房屋核查 - 在线表格解析器"""
from .base import BaseParser


class RentalCheckParser(BaseParser):
    parser_type = "出租房屋核查"
    table_name = "t_rental_check"
    COLUMNS = [
        "下发时间", "截止时间", "核查人", "社区", "姓名",
        "身份证号", "手机号码", "房屋地址", "现住址", "核查结果",
        "入住方式", "研判", "二次反馈",
    ]

    def get_business_key(self) -> list[str]:
        return ["身份证号", "手机号码"]

    def merge_duplicate_row(
        self,
        previous: dict,
        incoming: dict,
    ) -> dict | None:
        """同一核查对象在表中重复出现时只保留一条。

        身份证号和手机号都为空时无法确认是同一人，仍交给同步引擎报错。
        已确认是同一人的重复行按表格顺序合并：后行的非空内容优先，
        后行空白不会擦除前行已经填写的核查结果。
        """
        identity_values = [
            str(incoming.get(column, "") or "").strip()
            for column in self.get_business_key()
        ]
        if not any(identity_values):
            return None

        merged = dict(previous)
        for column in self.COLUMNS:
            value = str(incoming.get(column, "") or "").strip()
            if value:
                merged[column] = value
        return merged
