"""在线表格解析器注册中心"""
from .base import BaseParser, ColumnDef
from .default import DefaultParser
from .fullchain import FullChainParser
from .rental_check import RentalCheckParser
from .police_stats import PoliceStatsParser
from .suspect_unrevoked import SuspectUnrevokedParser
from .suspect_return import SuspectReturnParser
from .delivery_industry import DeliveryIndustryParser
from .group_rental import GroupRentalParser
from .suzhou_police import SuzhouPoliceParser
from .traffic_police import TrafficPoliceParser

# 解析器注册表：parser_type → 解析器类
PARSER_REGISTRY: dict[str, type[BaseParser]] = {
    "default": DefaultParser,
    "全链条": FullChainParser,
    "出租房屋核查": RentalCheckParser,
    "涉警统计": PoliceStatsParser,
    "疑似未注销模型三": SuspectUnrevokedParser,
    "疑似返苏": SuspectReturnParser,
    "寄递业": DeliveryIndustryParser,
    "群租房核查": GroupRentalParser,
    "苏州涉警": SuzhouPoliceParser,
    "交通涉警": TrafficPoliceParser,
}

SUPPORTED_TYPES = list(PARSER_REGISTRY.keys())

# parser_type → table_name 映射
TABLE_NAMES = {pt: cls.table_name for pt, cls in PARSER_REGISTRY.items() if pt != "default"}


def get_parser(parser_type: str) -> BaseParser:
    """根据类型获取解析器实例"""
    parser_cls = PARSER_REGISTRY.get(parser_type, DefaultParser)
    return parser_cls()
