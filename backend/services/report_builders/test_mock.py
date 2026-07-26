"""测试数据日报生成器 - 用于验证工作量统计逻辑"""

from .base import BaseReportBuilder


class TestMockBuilder(BaseReportBuilder):
    parser_type = "测试数据"
    source_table = "t_test_mock"
    table_suffix = "testMock"
    see_base_keywords = ["已登记", "无需登记", "离苏", "移交"]
