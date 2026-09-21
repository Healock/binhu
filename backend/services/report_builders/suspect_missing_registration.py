"""疑似漏登记日报生成器。"""

from .base import BaseReportBuilder


class SuspectMissingRegistrationBuilder(BaseReportBuilder):
    parser_type = "疑似漏登记"
    source_table = "t_suspect_missing_registration"
    table_suffix = "suspectMissingRegistration"
    result_column = "核查结果"
    checked_results = ("移交（所内）",)
    see_base_keywords = ("已登记", "离苏", "无需登记", "移交（所外）")

