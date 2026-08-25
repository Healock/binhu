"""在线表格管理 API"""

import re
from urllib.parse import urlparse, parse_qs
from fastapi import APIRouter, Depends, HTTPException, Request
from database import get_db
from deps import require_super_admin
from schemas.spreadsheet import SpreadsheetCreate, SpreadsheetUpdate, SpreadsheetResponse
from services.parsers import SUPPORTED_TYPES
from services.audit import record_admin_audit, request_audit_fields

# 固定表格类型（用户配置时按此列表展示，不可自由添加）
FIXED_TYPES = [
    "全链条",
    "出租房屋核查",
    "涉警统计",
    "疑似未注销模型三",
    "疑似返苏",
    "寄递业",
    "群租房核查",
    "苏州涉警",
    "交通涉警",
]

router = APIRouter(
    prefix="/api/spreadsheets",
    tags=["在线表格管理"],
    dependencies=[Depends(require_super_admin)],
)

# SELECT 列列表（复用）
_COLS = """id, name, url, file_id, data_sheet_id, summary_sheet_id,
           header_row, parser_type, enabled,
           DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%S'),
           DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%S')"""


def _row_to_response(r) -> SpreadsheetResponse:
    return SpreadsheetResponse(
        id=r[0], name=r[1], url=r[2], file_id=r[3], data_sheet_id=r[4],
        summary_sheet_id=r[5], header_row=r[6], parser_type=r[7], enabled=bool(r[8]),
        created_at=r[9], updated_at=r[10],
    )


def parse_tencent_doc_url(url: str) -> dict:
    """从腾讯文档 URL 解析 file_id 和 data_sheet_id

    示例: https://docs.qq.com/sheet/DZxxxxxxxxxxxx?tab=xxxxxx
    """
    file_id = ""
    data_sheet_id = "000001"

    match = re.search(r'/sheet/([A-Za-z0-9]+)', url)
    if match:
        file_id = match.group(1)

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if 'tab' in params and params['tab']:
        data_sheet_id = params['tab'][0]

    return {"file_id": file_id, "data_sheet_id": data_sheet_id}


@router.get("/meta/parser-types")
async def get_parser_types():
    """获取支持的解析器类型列表"""
    return {"data": SUPPORTED_TYPES}


@router.get("/config")
async def get_spreadsheets_config(conn=Depends(get_db)):
    """获取固定表格类型的 URL 配置。"""
    async with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(FIXED_TYPES))
        await cur.execute(
            f"SELECT {_COLS} FROM _config_spreadsheets WHERE parser_type IN ({placeholders})",
            FIXED_TYPES,
        )
        rows = await cur.fetchall()
    existing = {r[7]: _row_to_response(r) for r in rows}
    return {
        "data": [
            {"parser_type": t, "url": existing[t].url if t in existing else "",
             "id": existing[t].id if t in existing else None}
            for t in FIXED_TYPES
        ]
    }


@router.put("/config")
async def save_spreadsheets_config(
    payload: dict,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    """批量保存7种表格类型的URL配置

    payload: {"configs": {"全链条": "https://...", "出租房屋核查": "https://...", ...}}
    """
    configs = payload.get("configs", {})
    async with conn.cursor() as cur:
        for parser_type in FIXED_TYPES:
            url = (configs.get(parser_type) or "").strip()
            if not url:
                continue
            parsed = parse_tencent_doc_url(url)
            if not parsed["file_id"]:
                continue
            await cur.execute(f"SELECT {_COLS} FROM _config_spreadsheets WHERE parser_type = %s", (parser_type,))
            existing = await cur.fetchone()
            if existing:
                await cur.execute(
                    "UPDATE _config_spreadsheets SET url=%s, file_id=%s, data_sheet_id=%s WHERE parser_type=%s",
                    (url, parsed["file_id"], parsed["data_sheet_id"], parser_type),
                )
            else:
                await cur.execute(
                    """INSERT INTO _config_spreadsheets (name, url, file_id, data_sheet_id, parser_type, enabled)
                       VALUES (%s, %s, %s, %s, %s, 1)""",
                    (parser_type, url, parsed["file_id"], parsed["data_sheet_id"], parser_type),
                )
    await record_admin_audit(
        user,
        "spreadsheet.config.update",
        target_type="spreadsheet",
        target_name="batch",
        detail={"types": sorted(configs)},
        **request_audit_fields(request),
    )
    return {"message": "配置已保存"}


@router.get("", response_model=list[SpreadsheetResponse])
async def list_spreadsheets(conn=Depends(get_db)):
    """列出所有在线表格"""
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_COLS} FROM _config_spreadsheets ORDER BY id DESC")
        rows = await cur.fetchall()
    return [_row_to_response(r) for r in rows]


@router.post("", response_model=SpreadsheetResponse, status_code=201)
async def create_spreadsheet(
    data: SpreadsheetCreate,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    """添加在线表格（自动从URL解析file_id和子表ID）"""
    parsed = parse_tencent_doc_url(data.url)
    file_id = data.file_id or parsed["file_id"]
    data_sheet_id = parsed["data_sheet_id"] if data.data_sheet_id == "000001" else data.data_sheet_id

    if not file_id:
        raise HTTPException(status_code=400, detail="无法从URL解析file_id，请检查链接是否为腾讯文档表格")

    async with conn.cursor() as cur:
        try:
            await cur.execute(
                """INSERT INTO _config_spreadsheets
                   (name, url, file_id, data_sheet_id, summary_sheet_id, header_row, parser_type, enabled)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (data.name, data.url, file_id, data_sheet_id,
                 data.summary_sheet_id, data.header_row, data.parser_type, int(data.enabled)),
            )
            new_id = cur.lastrowid
            await cur.execute(f"SELECT {_COLS} FROM _config_spreadsheets WHERE id = %s", (new_id,))
            r = await cur.fetchone()
        except Exception as e:
            if "Duplicate" in str(e):
                raise HTTPException(status_code=400, detail="该URL已存在")
            raise
    await record_admin_audit(
        user,
        "spreadsheet.create",
        target_type="spreadsheet",
        target_name=str(new_id),
        detail={"name": data.name, "parser_type": data.parser_type},
        **request_audit_fields(request),
    )
    return _row_to_response(r)


@router.get("/{spreadsheet_id}", response_model=SpreadsheetResponse)
async def get_spreadsheet(spreadsheet_id: int, conn=Depends(get_db)):
    """获取单个在线表格"""
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_COLS} FROM _config_spreadsheets WHERE id = %s", (spreadsheet_id,))
        r = await cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="表格不存在")
    return _row_to_response(r)


@router.put("/{spreadsheet_id}", response_model=SpreadsheetResponse)
async def update_spreadsheet(
    spreadsheet_id: int,
    data: SpreadsheetUpdate,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    """更新在线表格"""
    updates = {}
    if data.name is not None:
        updates["name"] = data.name
    if data.url is not None:
        updates["url"] = data.url
        parsed = parse_tencent_doc_url(data.url)
        if parsed["file_id"]:
            updates["file_id"] = parsed["file_id"]
        updates["data_sheet_id"] = parsed["data_sheet_id"]
    if data.parser_type is not None:
        updates["parser_type"] = data.parser_type
    if data.data_sheet_id is not None:
        updates["data_sheet_id"] = data.data_sheet_id
    if data.summary_sheet_id is not None:
        updates["summary_sheet_id"] = data.summary_sheet_id
    if data.header_row is not None:
        updates["header_row"] = data.header_row
    if data.enabled is not None:
        updates["enabled"] = int(data.enabled)

    if not updates:
        raise HTTPException(status_code=400, detail="没有提供要更新的字段")

    async with conn.cursor() as cur:
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        params = list(updates.values()) + [spreadsheet_id]
        await cur.execute(f"UPDATE _config_spreadsheets SET {set_clause} WHERE id = %s", params)
        await cur.execute(f"SELECT {_COLS} FROM _config_spreadsheets WHERE id = %s", (spreadsheet_id,))
        r = await cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="表格不存在")
    await record_admin_audit(
        user,
        "spreadsheet.update",
        target_type="spreadsheet",
        target_name=str(spreadsheet_id),
        detail={"fields": sorted(updates)},
        **request_audit_fields(request),
    )
    return _row_to_response(r)


@router.delete("/{spreadsheet_id}")
async def delete_spreadsheet(
    spreadsheet_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    """删除在线表格"""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM _config_spreadsheets WHERE id = %s", (spreadsheet_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="表格不存在")
    await record_admin_audit(
        user,
        "spreadsheet.delete",
        target_type="spreadsheet",
        target_name=str(spreadsheet_id),
        **request_audit_fields(request),
    )
    return {"message": "删除成功"}
