"""寄递业日报生成器"""

from .base import BaseReportBuilder


class DeliveryIndustryBuilder(BaseReportBuilder):
    parser_type = "寄递业"
    source_table = "t_delivery_industry"
    table_suffix = "deliveryIndustry"
    see_base_keywords = ["已登记", "离苏", "无需登记", "移交", "身份错误"]
