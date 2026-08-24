"""苏州涉警 - 在线表格解析器。"""

import hashlib

from .base import BaseParser


class SuzhouPoliceParser(BaseParser):
    parser_type = "苏州涉警"
    table_name = "t_suzhou_police"
    COLUMNS = [
        "下发日期", "截止日期", "核查人", "社区", "姓名",
        "身份证号", "联系号码", "疑似现住址", "接警编号", "出警日期",
        "出警类别", "出警内容", "出警单位", "参考派出所", "现住址",
        "核查结果", "研判", "二次反馈",
    ]

    def get_business_key(self) -> list[str]:
        return ["身份证号", "联系号码", "下发日期"]

    def make_row_key(self, row: dict) -> str:
        case_number = str(row.get("接警编号", "") or "").strip()
        if case_number:
            return hashlib.md5(case_number.encode("utf-8")).hexdigest()
        return super().make_row_key(row)
