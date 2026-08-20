"""Pydantic schemas for sync"""

from pydantic import BaseModel, Field
from typing import Optional


class SyncScheduleStatus(BaseModel):
    enabled: bool = True
    interval_minutes: int = 10
    next_run_at: Optional[str] = None
    server_time: Optional[str] = None


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
    trigger_source: str = "manual"
    phase: str = "queued"
    current_item: Optional[str] = None
    total_steps: int = 0
    completed_steps: int = 0
    last_success_at: Optional[str] = None
    schedule: SyncScheduleStatus = Field(default_factory=SyncScheduleStatus)
