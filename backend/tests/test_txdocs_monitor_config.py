import os

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.stats import _txdocs_file_id, _txdocs_config_payload


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
