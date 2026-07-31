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
    DATABASE_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {}

    def get_schema(self) -> list[ColumnDef]:
        """返回列定义列表"""
        return [ColumnDef(name=col) for col in self.COLUMNS]

    def get_business_key(self) -> list[str]:
        """返回业务主键列名列表（用于增量比对）"""
        raise NotImplementedError(f"{self.__class__.__name__} 未实现 get_business_key")

    def resolve_database_columns(
        self, available_columns: set[str]
    ) -> dict[str, str]:
        """把代码中的标准列名映射到数据库当前实际列名。"""
        resolved: dict[str, str] = {}
        missing: list[str] = []

        for column in self.COLUMNS:
            candidates = (column, *self.DATABASE_COLUMN_ALIASES.get(column, ()))
            actual = next(
                (candidate for candidate in candidates if candidate in available_columns),
                None,
            )
            if actual is None:
                missing.append(column)
            else:
                resolved[column] = actual

        if missing:
            raise RuntimeError(
                f"{self.table_name} 缺少字段: {', '.join(missing)}"
            )
        return resolved

    def parse_row(self, raw_row: list) -> dict:
        """将腾讯文档的原始行（按列位置）解析为字典"""
        return {col: str(raw_row[i]).strip() if i < len(raw_row) else ""
                for i, col in enumerate(self.COLUMNS)}

    def make_row_key(self, row: dict) -> str:
        """从行数据生成业务主键 MD5"""
        key_parts = [str(row.get(k, "")) for k in self.get_business_key()]
        return hashlib.md5("|".join(key_parts).encode()).hexdigest()

    def merge_duplicate_row(
        self,
        previous: dict,
        incoming: dict,
    ) -> dict | None:
        """处理同一业务主键的不同内容。

        默认不允许合并，由同步引擎停止该表同步。只有业务规则已经明确的
        解析器才应覆盖此方法。
        """
        del previous, incoming
        return None
