"""默认解析器 - 宽表兜底（20列VARCHAR）"""
from .base import BaseParser


class DefaultParser(BaseParser):
    parser_type = "default"
    table_name = "t_default"
    COLUMNS = [f"列{i+1}" for i in range(20)]

    def get_business_key(self) -> list[str]:
        return ["列1", "列2"]
