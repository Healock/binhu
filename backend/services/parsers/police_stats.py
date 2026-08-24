"""涉警统计 - 在线表格解析器（仅raw入库，不进日报）"""
import hashlib

from .base import BaseParser


class PoliceStatsParser(BaseParser):
    parser_type = "涉警统计"
    table_name = "t_police_stats"
    COLUMNS = [
        "序号", "日期", "社区", "简要警情及处理结果", "是否开户",
        "现住址", "房屋属性", "居住时间", "房东信息", "二房东信息",
        "备注", "房东是否处罚",
    ]

    def get_business_key(self) -> list[str]:
        return ["序号", "日期", "社区", "简要警情及处理结果"]

    def make_row_key(self, row: dict) -> str:
        """20 位接警编号直接形成稳定行键，旧式序号保持原复合行键。"""
        case_number = str(row.get("序号", "") or "").strip()
        if len(case_number) == 20 and case_number.isdigit():
            return hashlib.md5(case_number.encode("utf-8")).hexdigest()
        return super().make_row_key(row)
