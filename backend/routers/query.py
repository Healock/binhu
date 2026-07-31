"""在线数据查询 API"""

import json
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from database import get_db
from services.parsers import PARSER_REGISTRY, get_parser
from services.schema_compat import get_database_column_map, quote_identifier
from deps import require_permission
from services.data_scope import community_names_for_scope, community_scope
from services.permissions import ONLINE_RAW_VIEW

router = APIRouter(prefix="/api/query", tags=["数据查询"])

QUERY_TYPES = [t for t in PARSER_REGISTRY.keys() if t != "default"]


@router.get("/types")
async def get_query_types(
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
):
    del user
    return {"data": QUERY_TYPES}


@router.get("/{parser_type}")
async def query_data(
    parser_type: str,
    source: str = Query("online"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    keyword: Optional[str] = Query(None),
    sort_by: Optional[str] = Query(None),
    sort_order: str = Query("desc"),
    filters: Optional[str] = Query(None, description='JSON: {"列名": ["值1","值2"]}'),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    """分页查询，支持关键词搜索 + 按列筛选 + 排序"""
    if parser_type not in PARSER_REGISTRY or parser_type == "default":
        raise HTTPException(status_code=400, detail=f"不支持的类型: {parser_type}")

    parser = get_parser(parser_type)
    table = parser.table_name
    columns = parser.COLUMNS

    if source == "archive":
        table = f"OnlineDataArchive.{table}_archive"

    column_map = await get_database_column_map(conn, table, parser)

    def database_column(column: str) -> str:
        return quote_identifier(column_map[column])

    col_list = ", ".join(database_column(column) for column in columns)

    # 构建 WHERE
    where_parts = []
    params = []

    if not isinstance(user, dict):
        user = {"data_scope": "all"}
    scope = community_scope(user)
    if scope is not None:
        allowed_communities = await community_names_for_scope(conn, scope)
        if not allowed_communities or "社区" not in columns:
            where_parts.append("1=0")
        else:
            placeholders = ",".join(["%s"] * len(allowed_communities))
            where_parts.append(
                f"{database_column('社区')} IN ({placeholders})"
            )
            params.extend(allowed_communities)

    if keyword:
        like_conditions = " OR ".join(
            f"{database_column(column)} LIKE %s" for column in columns
        )
        where_parts.append(f"({like_conditions})")
        params.extend([f"%{keyword}%"] * len(columns))

    if filters:
        try:
            filter_dict = json.loads(filters)
            for col, vals in filter_dict.items():
                if col in columns and vals:
                    placeholders = ",".join(["%s"] * len(vals))
                    where_parts.append(
                        f"{database_column(col)} IN ({placeholders})"
                    )
                    params.extend(vals)
        except (json.JSONDecodeError, TypeError):
            pass

    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    async with conn.cursor() as cur:
        await cur.execute(f"SELECT COUNT(*) FROM {table}{where}", params)
        count_row = await cur.fetchone()
        total = count_row[0] if count_row else 0

        offset = (page - 1) * page_size
        if sort_by and sort_by in columns:
            order_clause = (
                f"ORDER BY {database_column(sort_by)} "
                f"{'ASC' if sort_order == 'asc' else 'DESC'}"
            )
        else:
            order_clause = "ORDER BY id DESC"
        await cur.execute(
            f"SELECT {col_list} FROM {table}{where} {order_clause} LIMIT %s OFFSET %s",
            params + [page_size, offset],
        )
        rows = await cur.fetchall()

    data = []
    for row in rows:
        record = {}
        for i, col in enumerate(columns):
            record[col] = str(row[i]) if row[i] is not None else ""
        data.append(record)

    result = {
        "data": data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "columns": columns,
    }
    if scope == "":
        result["scope_message"] = "当前账号尚未分配社区部门，暂无业务数据"
    return result
