"""疑似未注销模型三 - 在线表格解析器"""
from .base import BaseParser


class SuspectUnrevokedParser(BaseParser):
    parser_type = "疑似未注销模型三"
    table_name = "t_suspect_unrevoked"
    COLUMNS = [
        "截止时间", "核查人", "姓名", "身份证号", "联系方式",
        "地址", "下发社区", "核查结果", "备注",
    ]
    COMMUNITY_COLUMN = "下发社区"

    def get_business_key(self) -> list[str]:
        return ["身份证号", "联系方式"]
