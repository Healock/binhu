"""数据库行映射 dataclass"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class Spreadsheet:
    id: Optional[int] = None
    name: str = ""
    url: str = ""
    file_id: str = ""
    data_sheet_id: str = "000001"
    summary_sheet_id: str = "汇总"
    header_row: int = 1
    parser_type: str = "default"
    enabled: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class RawData:
    id: Optional[int] = None
    spreadsheet_id: int = 0
    row_number: int = 0
    下发日期: Optional[str] = None
    截止日期: Optional[str] = None
    核查人: Optional[str] = None
    社区: Optional[str] = None
    来源: Optional[str] = None
    姓名: Optional[str] = None
    身份证号: Optional[str] = None
    电话号码: Optional[str] = None
    地址: Optional[str] = None
    创建时间: Optional[str] = None
    现住址: Optional[str] = None
    核查结果: Optional[str] = None
    研判: Optional[str] = None
    二次反馈: Optional[str] = None
    fetched_at: Optional[str] = None


@dataclass
class DailyStats:
    id: Optional[int] = None
    spreadsheet_id: int = 0
    核查人: str = ""
    下发日期: str = ""
    数据总数: int = 0
    已核查: int = 0
    未核查: int = 0
    核查完成率: float = 0.0
    无法核实: int = 0
    移交: int = 0
    已登记: int = 0
    通勤: int = 0
    离苏: int = 0
    空白: int = 0
    无法见底数: int = 0
    核查见底率: float = 0.0
    computed_at: Optional[str] = None


@dataclass
class OAuthToken:
    id: Optional[int] = None
    client_id: str = ""
    client_secret: str = ""
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    open_id: Optional[str] = None
    expires_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class SyncLog:
    id: Optional[int] = None
    status: str = "pending"
    total_rows: int = 0
    processed_rows: int = 0
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
