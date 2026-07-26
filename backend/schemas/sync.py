"""Pydantic schemas for sync"""

from pydantic import BaseModel
from typing import Optional


class SyncTriggerResponse(BaseModel):
    task_id: int
    status: str
    message: str


class SyncStatusResponse(BaseModel):
    task_id: int
    status: str
    total_rows: int = 0
    processed_rows: int = 0
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
