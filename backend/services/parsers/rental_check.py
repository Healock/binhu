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
