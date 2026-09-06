"""Strict projection from existing domain Outbox to Kafka v1 metadata."""
from __future__ import annotations
from datetime import datetime, timezone
from .domain_events import decode_event_row
from .kafka_event_contract import validate_task_event, EventContractError
_EVENT_MAP={"task.saved":"task.saved","task.claimed":"task.claimed","task.assigned":"task.assigned","task.reviewed":"task.reviewed","task.archived":"task.archived","task.created":"task.created","task.deleted":"task.deleted"}
def _utc_z(value):
 dt=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace("Z","+00:00"))
 if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
 return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z")
def domain_row_to_task_event(row, *, run_id, environment="shadow"):
 source=decode_event_row(row)
 if environment!="shadow" or not run_id.startswith("KSHADOW-"): raise EventContractError("shadow identity")
 if source["event_type"] not in _EVENT_MAP: raise EventContractError("event_type")
 if not source.get("task_id") or source.get("source_id") is None or not source.get("operation_id"): raise EventContractError("task source mapping required")
 return validate_task_event({"schema_version":1,"event_id":source["event_id"],"event_type":_EVENT_MAP[source["event_type"]],"task_id":source["task_id"],"source_id":source["source_id"],"revision":source["aggregate_revision"],"operation_id":source["operation_id"],"changed_fields":source.get("changed_fields",[]),"timestamp":_utc_z(source["occurred_at"]),"environment":environment,"run_id":run_id})
