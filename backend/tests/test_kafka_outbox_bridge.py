import pytest
from datetime import datetime, timezone
from services.kafka_outbox_bridge import domain_row_to_task_event
def row(**changes):
 d=dict(event_id="00000000-0000-0000-0000-000000000001",schema_version=2,domain="online",event_type="task.saved",aggregate_type="task",aggregate_id="t_fullchain:9",aggregate_revision=5,audiences_json='["authenticated"]',status="pending",attempt_count=0,available_at=None,locked_by=None,locked_until=None,last_error_code="",last_error_summary="",occurred_at=datetime(2026,9,7,tzinfo=timezone.utc),published_at=None,task_id="t_fullchain:9",source_id=9,operation_id="00000000-0000-0000-0000-000000000002",changed_fields_json='["task_state"]'); d.update(changes); return tuple(d[k] for k in ["event_id","schema_version","domain","event_type","aggregate_type","aggregate_id","aggregate_revision","audiences_json","status","attempt_count","available_at","locked_by","locked_until","last_error_code","last_error_summary","occurred_at","published_at","task_id","source_id","operation_id","changed_fields_json"])
def test_converts_only_metadata():
 e=domain_row_to_task_event(row(),run_id="KSHADOW-test"); assert e["task_id"]=="t_fullchain:9" and e["changed_fields"]==["task_state"]
@pytest.mark.parametrize("change",[{"task_id":None},{"source_id":None},{"operation_id":None},{"event_type":"unapproved"},{"changed_fields_json":"[\"phone\"]"}])
def test_rejects_unmappable_or_unsafe_rows(change):
 with pytest.raises(ValueError): domain_row_to_task_event(row(**change),run_id="KSHADOW-test")
