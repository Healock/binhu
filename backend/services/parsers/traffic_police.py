"""交通涉警 - 在线表格解析器。"""

from .base import BaseParser


class TrafficPoliceParser(BaseParser):
    parser_type = "交通涉警"
    table_name = "t_traffic_police"
    COLUMNS = [
        "下发日期", "截止日期", "核查人", "社区", "姓名", "身份证号",
        "联系号码", "地址1", "现住址", "核查结果", "研判", "二次反馈",
    ]

    def get_business_key(self) -> list[str]:
        return ["身份证号", "联系号码", "下发日期"]
