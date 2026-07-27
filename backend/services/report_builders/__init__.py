"""日报生成器注册中心"""

from .base import BaseReportBuilder
from .fullchain import FullChainBuilder
from .rental_check import RentalCheckBuilder
from .delivery_industry import DeliveryIndustryBuilder
from .suspect_unrevoked import SuspectUnrevokedBuilder
from .suspect_return import SuspectReturnBuilder

# 注册表：parser_type → builder实例
BUILDERS: dict[str, BaseReportBuilder] = {
    "全链条": FullChainBuilder(),
    "出租房屋核查": RentalCheckBuilder(),
    "寄递业": DeliveryIndustryBuilder(),
    "疑似未注销模型三": SuspectUnrevokedBuilder(),
    "疑似返苏": SuspectReturnBuilder(),
}

# 已实现的类型
IMPLEMENTED_TYPES = list(BUILDERS.keys())


def get_builder(parser_type: str) -> BaseReportBuilder | None:
    return BUILDERS.get(parser_type)
