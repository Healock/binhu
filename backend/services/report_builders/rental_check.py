"""出租房屋核查日报生成器"""

from .base import BaseReportBuilder


class RentalCheckBuilder(BaseReportBuilder):
    parser_type = "出租房屋核查"
    source_table = "t_rental_check"
    table_suffix = "rentalHouse"
    see_base_keywords = ["已登记", "无需登记", "离苏", "常口", "移交"]
