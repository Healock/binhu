import os
from pathlib import Path

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from unittest.mock import patch

from routers.stats import (
    _manual_txdocs_run_response,
    _txdocs_config_payload,
    _txdocs_file_id,
)


def test_monitor_url_only_accepts_https_qq_sheet_links():
    assert _txdocs_file_id("https://docs.qq.com/sheet/abc_123") == "abc_123"
    assert _txdocs_file_id("https://docs.qq.com/sheet/abc_123?tab=BB08J2") == "abc_123"
    for value in ("http://docs.qq.com/sheet/x", "https://example.com/sheet/x", "https://docs.qq.com/doc/x"):
        try:
            _txdocs_file_id(value)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 400
        else:
            raise AssertionError("invalid URL accepted")


def test_monitor_config_payload_never_returns_token():
    payload = _txdocs_config_payload({
        "enabled": True, "spreadsheet_url": "https://docs.qq.com/sheet/demo", "file_id": "demo",
        "sheet_id": "sheet1", "header_row": 1, "parser_type": "全链条",
        "client_id": "client", "access_token": "super-secret", "open_id": "open",
        "interval_seconds": 600,
    })
    assert payload["configured"] is True
    assert "access_token" not in payload
    assert payload["access_token_configured"] is True


def test_monitor_config_uses_environment_identity_as_hard_boundary():
    target = {
        "enabled": True, "spreadsheet_url": "https://docs.qq.com/sheet/demo", "file_id": "demo",
        "sheet_id": "sheet1", "header_row": 1, "parser_type": "全链条",
        "interval_seconds": 600,
    }
    with patch("services.txdocs_statistics_monitor.settings.APP_ENVIRONMENT", "production"):
        production = _txdocs_config_payload(target, {"client_id": "c", "access_token": "t", "open_id": "o"})
    with patch("services.txdocs_statistics_monitor.settings.APP_ENVIRONMENT", "staging"):
        staging = _txdocs_config_payload(target, {"client_id": "c", "access_token": "t", "open_id": "o"})
    assert production["environment_allowed"] is True
    assert production["enabled"] is True
    assert staging["environment_allowed"] is False
    assert staging["enabled"] is False


def test_non_production_configuration_is_not_writable():
    source = Path(__file__).parents[1].joinpath("routers", "stats.py").read_text(encoding="utf-8")
    marker = "async def update_txdocs_monitor_config"
    body = source[source.index(marker):]
    guard = body.index("if not monitoring_environment_allowed()")
    mutation = body.index("file_id = _txdocs_file_id", guard)
    assert guard < mutation


def test_manual_monitor_run_rejects_zero_successful_sources():
    try:
        _manual_txdocs_run_response(0)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 502
        assert "未取得任何成功快照" in str(getattr(exc, "detail", ""))
    else:
        raise AssertionError("zero successful sources must not be reported as success")


def test_manual_monitor_run_reports_successful_source_count():
    result = _manual_txdocs_run_response(2)
    assert result == {
        "successful_sources": 2,
        "message": "已完成一次只读读取（成功目标 2 个）",
    }
