import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.diagnostics import CheckResult, _request_summary, ensure_diagnostic_schema_sql
from services.ops_redaction import redact_text


class _Request:
    method = "POST"
    url = type("URL", (), {"path": "/api/tasks/save"})()
    query_params = {"task_id": "abc", "password": "hidden"}


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
