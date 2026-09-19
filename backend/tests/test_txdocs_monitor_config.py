import os
from pathlib import Path

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from unittest.mock import patch

from routers.stats import (
    _manual_txdocs_run_response,
    _normalize_txdocs_credentials,
    _txdocs_config_payload,
    _txdocs_file_id,
    TxDocsMonitorCredentialsUpdate,
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


def test_monitor_credentials_require_one_complete_authorization_triplet():
    payload = TxDocsMonitorCredentialsUpdate(
        client_id=" client-id ",
        access_token=" access-token ",
        open_id=" open-id ",
    )
    assert _normalize_txdocs_credentials(payload) == (
        "client-id",
        "access-token",
        "open-id",
    )

    incomplete = TxDocsMonitorCredentialsUpdate(
        client_id="client-id",
        access_token="   ",
        open_id="open-id",
    )
    try:
        _normalize_txdocs_credentials(incomplete)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
        assert "同一次腾讯授权" in str(getattr(exc, "detail", ""))
    else:
        raise AssertionError("whitespace-only credentials must be rejected")


def test_monitor_credentials_have_a_separate_redacted_save_endpoint():
    source = Path(__file__).parents[1].joinpath("routers", "stats.py").read_text(encoding="utf-8")
    marker = '@router.put("/txdocs-monitor/config/credentials")'
    assert marker in source
    body = source[source.index(marker):source.index('@router.put("/txdocs-monitor/config")', source.index(marker) + len(marker))]
    assert "_store_txdocs_monitor_credentials" in body
    assert '"txdocs.monitor.credentials.update"' in body
    assert 'detail={"credential_fields": 3}' in body
    assert 'detail={"client_id"' not in body
    assert 'detail={"access_token"' not in body


def test_target_save_does_not_rewrite_credentials_when_fields_are_omitted():
    source = Path(__file__).parents[1].joinpath("routers", "stats.py").read_text(encoding="utf-8")
    marker = 'async def update_txdocs_monitor_config('
    body = source[source.index(marker):source.index('@router.post("/txdocs-monitor/config/disable")')]
    assert "credentials_update_requested = bool(" in body
    assert "if credentials_update_requested:" in body
    assert body.index("if credentials_update_requested:") < body.index(
        "await _store_txdocs_monitor_credentials("
    )


def test_manual_monitor_run_rejects_zero_successful_sources():
    try:
        _manual_txdocs_run_response(0)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 502
        assert "未取得任何成功快照" in str(getattr(exc, "detail", ""))
    else:
        raise AssertionError("zero successful sources must not be reported as success")


def test_manual_monitor_run_explains_invalid_authorization_triplet():
    try:
        _manual_txdocs_run_response(0, ["txdocs_400006"])
    except Exception as exc:
        detail = str(getattr(exc, "detail", ""))
        assert getattr(exc, "status_code", None) == 502
        assert "400006" in detail
        assert "Client ID" in detail
        assert "Access Token" in detail
        assert "Open ID" in detail
    else:
        raise AssertionError("invalid Tencent authorization must be actionable")


def test_manual_monitor_run_explains_sheet_layout_failure():
    try:
        _manual_txdocs_run_response(0, ["invalid_sheet_layout"])
    except Exception as exc:
        detail = str(getattr(exc, "detail", ""))
        assert getattr(exc, "status_code", None) == 502
        assert "表头行号" in detail
        assert "列名" in detail
    else:
        raise AssertionError("invalid sheet layout must be actionable")


def test_manual_monitor_run_reports_successful_source_count():
    result = _manual_txdocs_run_response(2)
    assert result == {
        "successful_sources": 2,
        "message": "已完成一次只读读取（成功目标 2 个）",
    }
