"""全链条 - 在线表格解析器"""
from .base import BaseParser


class FullChainParser(BaseParser):
    parser_type = "全链条"
    table_name = "t_fullchain"
    COLUMNS = [
        "下发日期", "截止日期", "核查人", "社区", "来源",
        "姓名", "身份证号", "电话号码", "地址", "创建时间",
        "现住址", "核查结果", "研判", "二次反馈",
    ]

    def get_business_key(self) -> list[str]:
        return ["身份证号", "电话号码", "下发日期"]
