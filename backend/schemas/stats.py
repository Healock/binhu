"""Pydantic schemas for statistics"""

from pydantic import BaseModel
from typing import Optional


class StatsItem(BaseModel):
    核查人: str
    下发日期: str
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


class StatsResponse(BaseModel):
    data: list[StatsItem]
    total: int
    page: int
    page_size: int


class DateRangeResponse(BaseModel):
    min_date: Optional[str] = None
    max_date: Optional[str] = None
