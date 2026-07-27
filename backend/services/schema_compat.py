"""数据库字段兼容工具。"""


def quote_identifier(identifier: str) -> str:
    """安全引用由项目代码定义的 MySQL 标识符。"""
    return f"`{identifier.replace('`', '``')}`"


async def get_database_column_map(conn, table: str, parser) -> dict[str, str]:
    """读取真实表结构，并返回标准列名到实际列名的映射。"""
    async with conn.cursor() as cur:
        await cur.execute(f"SHOW COLUMNS FROM {table}")
        rows = await cur.fetchall()

    available_columns = {str(row[0]) for row in rows}
    return parser.resolve_database_columns(available_columns)
