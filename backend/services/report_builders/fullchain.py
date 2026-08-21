"""全链条日报生成器"""

from .base import BaseReportBuilder


class FullChainBuilder(BaseReportBuilder):
    parser_type = "全链条"
    source_table = "t_fullchain"
    table_suffix = "fullChain"
    see_base_keywords = ["已登记", "待登记", "无需登记", "离苏", "移交", "移交（所内）", "移交（所外）"]
