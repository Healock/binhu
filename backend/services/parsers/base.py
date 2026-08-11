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
    MOBILE_EDITABLE_FIELDS: tuple[str, ...] = ()
    SOURCE_COLUMN_LAYOUTS: tuple[tuple[str, ...], ...] = ()
    DATABASE_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {}
    COMMUNITY_COLUMN = "社区"
    ALLOW_SOURCE_CONFLICTS = False

    def get_schema(self) -> list[ColumnDef]:
        """返回列定义列表"""
        return [ColumnDef(name=col) for col in self.COLUMNS]

    def source_column_layouts(self) -> list[list[str]]:
        """返回腾讯来源表支持的物理列布局，按优先级排列。"""
        if self.SOURCE_COLUMN_LAYOUTS:
            return [list(layout) for layout in self.SOURCE_COLUMN_LAYOUTS]
        return [list(self.COLUMNS)]

    def normalize_source_row(self, row: dict) -> dict[str, str]:
        """从含兼容占位列的腾讯来源行提取正式业务字段。"""
        return {
            column: str(row.get(column, "") or "").strip()
            for column in self.COLUMNS
        }

    @staticmethod
    def source_row_values(
        row: dict[str, str],
        source_columns: list[str],
    ) -> list[str]:
        """按腾讯物理列布局构造整行写入值。"""
        return [str(row.get(column, "") or "") for column in source_columns]

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

    def community_value(self, row: dict) -> str:
        """返回权限判断使用的来源社区。"""
        return str(row.get(self.COMMUNITY_COLUMN, "") or "").strip()

    def validate_new_row(self, row: dict) -> None:
        """校验平台新增行：全部业务主键和社区都必须完整。"""
        missing_keys = [
            key
            for key in self.get_business_key()
            if not str(row.get(key, "") or "").strip()
        ]
        if missing_keys:
            raise ValueError(f"请填写业务主键字段：{'、'.join(missing_keys)}")
        if self.COMMUNITY_COLUMN in self.COLUMNS and not self.community_value(row):
            raise ValueError("社区不能为空")

    def validate_existing_row_key(self, row: dict) -> None:
        """校验已有来源行仍可识别，并允许逐格修复历史不完整主键。"""
        values = [
            str(row.get(key, "") or "").strip()
            for key in self.get_business_key()
        ]
        if not any(values):
            raise ValueError("业务主键字段不能全部为空")

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
