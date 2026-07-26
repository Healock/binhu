"""在线表格解析器基类"""
import hashlib
from dataclasses import dataclass


@dataclass
class ColumnDef:
    """列定义"""
    name: str
    db_type: str = "VARCHAR(500)"
    is_key: bool = False


class BaseParser:
    """解析器基类 - 子类需定义 COLUMNS、table_name、get_business_key"""
    parser_type: str = "base"
    table_name: str = ""
    COLUMNS: list[str] = []

    def get_schema(self) -> list[ColumnDef]:
        """返回列定义列表"""
        return [ColumnDef(name=col) for col in self.COLUMNS]

    def get_business_key(self) -> list[str]:
        """返回业务主键列名列表（用于增量比对）"""
        raise NotImplementedError(f"{self.__class__.__name__} 未实现 get_business_key")

    def parse_row(self, raw_row: list) -> dict:
        """将腾讯文档的原始行（按列位置）解析为字典"""
        return {col: str(raw_row[i]).strip() if i < len(raw_row) else ""
                for i, col in enumerate(self.COLUMNS)}

    def make_row_key(self, row: dict) -> str:
        """从行数据生成业务主键 MD5"""
        key_parts = [str(row.get(k, "")) for k in self.get_business_key()]
        return hashlib.md5("|".join(key_parts).encode()).hexdigest()
