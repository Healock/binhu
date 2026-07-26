"""疑似返苏日报生成器"""

from .base import BaseReportBuilder


class SuspectReturnBuilder(BaseReportBuilder):
    parser_type = "疑似返苏"
    source_table = "t_suspect_return"
    table_suffix = "suspectReturn"
    result_column = "核查反馈"  # 注意：字段名是"核查反馈"不是"核查结果"
    see_base_keywords = ["已登记", "无需登记", "移交"]  # 3种（无"离苏"）
