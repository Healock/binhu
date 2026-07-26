"""寄递业 - 在线表格解析器"""
from .base import BaseParser


class DeliveryIndustryParser(BaseParser):
    parser_type = "寄递业"
    table_name = "t_delivery_industry"
    COLUMNS = [
        "下发时间", "截止时间", "核查人", "姓名", "身份证号",
        "地址1", "手机号码", "社区", "参考姓名", "参考身份证号码",
        "现住址", "核查结果", "研判", "二次反馈",
    ]

    def get_business_key(self) -> list[str]:
        return ["身份证号", "手机号码"]
