from __future__ import annotations

from datetime import date

from migrations.current_flow_cleanup import ARCHIVE_REASON, flow_specs, parse_business_date
from services.task_workflow import TASK_WORKFLOWS


def test_cleanup_scope_contains_every_current_workflow_once():
    specs = flow_specs()
    assert {item["parser_type"] for item in specs} == set(TASK_WORKFLOWS)
    assert len({item["table"] for item in specs}) == len(specs)
    assert all(item["archive_table"] == f"{item['table']}_archive" for item in specs)


def test_maintenance_date_is_evidence_only_and_parses_explicit_dates():
    assert parse_business_date("2026-10-16") == date(2026, 10, 16)
    assert parse_business_date("2026-10-16 12:30:00") == date(2026, 10, 16)
    assert parse_business_date("2026-10-16T12:30:00") == date(2026, 10, 16)
    assert parse_business_date("2026-13-99") is None
    assert ARCHIVE_REASON == "current_flow_cleanup_all"
