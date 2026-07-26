"""群租房核查 - 在线表格解析器（仅raw入库，不进日报）"""
from .base import BaseParser


class GroupRentalParser(BaseParser):
    parser_type = "群租房核查"
    table_name = "t_group_rental"
    COLUMNS = [
        "核查人", "社区", "出租屋编号", "出租屋地址", "更新时间",
        "居住证_居住人数", "居住证_间数", "居住证_床位数",
        "核查_人数", "核查_房间数", "核查_床位数",
        "入户走访", "走访日期", "星级评定", "责任书签订", "实际情况",
    ]

    def get_business_key(self) -> list[str]:
        return ["出租屋编号", "社区"]
