import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import diagnostics
from services.diagnostics import CheckResult, _request_summary, ensure_diagnostic_schema_sql
from services.ops_redaction import redact_text


class _Request:
    method = "POST"
    url = type("URL", (), {"path": "/api/tasks/save"})()
    query_params = {"task_id": "abc", "password": "hidden"}
    scope = {"route": type("Route", (), {"path": "/api/tasks/{task_id}"})()}


def test_request_summary_keeps_only_safe_identifiers():
    summary = _request_summary(_Request())
    assert summary["method"] == "POST"
    assert summary["path"] == "/api/tasks/save"
    assert summary["identifiers"] == {"task_id": "abc"}


def test_redaction_removes_credentials():
    assert "secret" not in redact_text('token="secret"').lower()
    assert "[REDACTED]" in redact_text('token="secret"')


def test_schema_contains_retention_and_safe_indexes():
    jobs, reports = ensure_diagnostic_schema_sql()
    assert "expires_at" in jobs
    assert "idx_diagnostic_user_created" in jobs
    assert "technical_json" in reports
    assert CheckResult("x", "healthy", "ok", {}).status == "healthy"


def test_expected_business_responses_do_not_create_incident_samples(monkeypatch):
    monkeypatch.setattr(diagnostics, "_incident_status_counts", diagnostics.Counter())
    assert diagnostics.should_capture_incident(_Request(), 404) is False
    assert diagnostics.should_capture_incident(_Request(), 409) is False
    snapshot = diagnostics.incident_capture_snapshot()
    assert snapshot["expected_by_status"]["404"] == 1
    assert snapshot["expected_by_status"]["409"] == 1


def test_server_incidents_are_sampled_once_per_route_and_minute(monkeypatch):
    monkeypatch.setattr(diagnostics, "_incident_samples", {})
    monkeypatch.setattr(diagnostics, "_captured_incidents", 0)
    monkeypatch.setattr(diagnostics, "_suppressed_duplicate_incidents", 0)
    monkeypatch.setattr(diagnostics.time, "time", lambda: 120.0)
    assert diagnostics.should_capture_incident(_Request(), 503) is True
    assert diagnostics.should_capture_incident(_Request(), 503) is False
    snapshot = diagnostics.incident_capture_snapshot()
    assert snapshot["captured_incident_count"] == 1
    assert snapshot["suppressed_duplicate_count"] == 1
