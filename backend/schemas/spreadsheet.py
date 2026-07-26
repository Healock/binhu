"""Pydantic schemas for spreadsheet CRUD"""

from pydantic import BaseModel, Field
from typing import Optional


class SpreadsheetCreate(BaseModel):
    name: str = Field(..., description="表格别名")
    url: str = Field(..., description="腾讯文档在线表格链接")
    parser_type: str = Field(default="default", description="解析器类型")
    file_id: str = Field(default="", description="腾讯文档fileId（后端从URL自动解析）")
    data_sheet_id: str = Field(default="000001", description="数据子表ID（后端从URL自动解析）")
    summary_sheet_id: str = Field(default="汇总", description="汇总子表名称")
    header_row: int = Field(default=1, description="表头行号")
    enabled: bool = Field(default=True, description="是否启用")


class SpreadsheetUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    parser_type: Optional[str] = None
    data_sheet_id: Optional[str] = None
    summary_sheet_id: Optional[str] = None
    header_row: Optional[int] = None
    enabled: Optional[bool] = None


class SpreadsheetResponse(BaseModel):
    id: int
    name: str
    url: str
    file_id: str
    data_sheet_id: str
    summary_sheet_id: str
    header_row: int
    parser_type: str
    enabled: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
