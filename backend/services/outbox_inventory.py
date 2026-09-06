"""Authoritative inventory for event-bus Outbox migration planning."""
from __future__ import annotations
from dataclasses import dataclass
@dataclass(frozen=True)
class OutboxSource:
 table: str; kind: str; topic: str | None; metadata_fields: tuple[str,...]; body_allowed: bool=False
SOURCES=(OutboxSource("_domain_event_outbox","task_domain","binhu.task.events.v1",("event_id","task_id","source_id","revision","operation_id","changed_fields","timestamp")),OutboxSource("photo_sheet_outbox","photo_writeback",None,("outbox_id","source_id","work_order_id","action","request_id","timestamp")),OutboxSource("_venue_cloud_outbox","venue_sync",None,("outbox_id","venue_id","config_revision","action","request_id","timestamp")),OutboxSource("_online_projection_jobs","derived_queue",None,("job_id","parser_type","row_key","revision","timestamp")))
BY_TABLE={item.table:item for item in SOURCES}
def source(table: str)->OutboxSource:
 try:return BY_TABLE[table]
 except KeyError:raise ValueError("unknown Outbox source") from None
def migration_ready(table: str)->bool:return table=="_domain_event_outbox"
