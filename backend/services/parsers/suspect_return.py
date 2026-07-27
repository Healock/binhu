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
