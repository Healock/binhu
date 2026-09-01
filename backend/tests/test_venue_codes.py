import os
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
import routers.venue_codes as venue_codes
from routers.venue_codes import (
    _check_form_token,
    _form_token,
    _public_venue_url,
    _token_digest,
    _validate_photo,
    delete_venue,
    venue_qrcode,
)


def test_venue_token_is_signed_and_expires():
    token = _form_token(12, 1_800_000_000)
    assert _check_form_token(token, 12) is False
    fresh = _form_token(12, __import__("time").time().__floor__())
    assert _check_form_token(fresh, 12)
    assert _token_digest("abc") != _token_digest("def")


def test_photo_magic_validation():
    mime, size, digest = _validate_photo("a.jpg", "image/jpeg", b"\xff\xd8\xff" + b"x")
    assert mime == "image/jpeg" and size == 4 and len(digest) == 64


def test_public_venue_url_is_absolute_and_normalizes_trailing_slash(monkeypatch):
    monkeypatch.setattr(settings, "VENUE_CLOUD_SYNC_ENABLED", False)
    monkeypatch.setattr(settings, "PUBLIC_WEB_BASE_URL", "https://portal.example.test/")

    assert _public_venue_url("token_value") == "https://portal.example.test/venue/token_value"


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "/relative",
        "http://portal.example.test",
        "https://user:pass@portal.example.test",
        "https://portal.example.test?next=other",
    ],
)
def test_public_venue_url_rejects_missing_or_unsafe_configuration(monkeypatch, base_url):
    monkeypatch.setattr(settings, "VENUE_CLOUD_SYNC_ENABLED", False)
    monkeypatch.setattr(settings, "PUBLIC_WEB_BASE_URL", base_url)

    with pytest.raises(HTTPException) as exc_info:
        _public_venue_url("token")

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_qrcode_png_encodes_the_same_absolute_public_url(monkeypatch):
    encoded_values: list[str] = []

    class FakeImage:
        def save(self, output: BytesIO, format: str) -> None:
            assert format == "PNG"
            output.write(b"png")

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, _query, _params):
            return None

        async def fetchone(self):
            return (
                7, "测试场所", "", "", None, "", "active", "digest", "encrypted", 1, None, None,
                1, 1, "local_only", None, None, None, None,
            )

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(settings, "VENUE_CLOUD_SYNC_ENABLED", False)
    monkeypatch.setattr(settings, "PUBLIC_WEB_BASE_URL", "https://portal.example.test")
    monkeypatch.setattr(venue_codes, "decrypt_secret", lambda _value: "public_token")
    monkeypatch.setitem(
        sys.modules,
        "qrcode",
        SimpleNamespace(make=lambda value: encoded_values.append(value) or FakeImage()),
    )

    response = await venue_qrcode(7, format="png", user={}, conn=FakeConnection())

    assert response.media_type == "image/png"
    assert encoded_values == ["https://portal.example.test/venue/public_token"]


@pytest.mark.asyncio
async def test_cloud_qrcode_requires_current_revision_confirmation(monkeypatch):
    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, _query, _params):
            return None

        async def fetchone(self):
            return (
                7, "测试场所", "", "", None, "", "active", "digest", "encrypted", 1, None, None,
                3, 1, "confirmed", 2, None, None, None,
            )

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(settings, "VENUE_CLOUD_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "VENUE_PUBLIC_BASE_URL", "https://venue-cloud.example.test")

    with pytest.raises(HTTPException) as exc_info:
        await venue_qrcode(7, format="json", user={}, conn=FakeConnection())

    assert exc_info.value.status_code == 409
    assert "当前版本" in exc_info.value.detail


def test_public_venue_url_uses_cloud_origin_only_when_sync_enabled(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_WEB_BASE_URL", "https://platform.example.test")
    monkeypatch.setattr(settings, "VENUE_PUBLIC_BASE_URL", "https://venue-cloud.example.test")
    monkeypatch.setattr(settings, "VENUE_CLOUD_SYNC_ENABLED", False)
    assert _public_venue_url("token") == "https://platform.example.test/venue/token"
    monkeypatch.setattr(settings, "VENUE_CLOUD_SYNC_ENABLED", True)
    assert _public_venue_url("token") == "https://venue-cloud.example.test/venue/token"


@pytest.mark.asyncio
async def test_delete_venue_soft_deletes_without_removing_visit_history(monkeypatch):
    statements: list[tuple[str, tuple]] = []

    class FakeCursor:
        rowcount = 1
        fetch_results = [(4, None)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, query, params):
            statements.append((query, params))

        async def fetchone(self):
            return self.fetch_results.pop(0)

    class FakeConnection:
        began = False
        committed = False
        rolled_back = False

        def cursor(self):
            return FakeCursor()

        async def begin(self):
            self.began = True

        async def commit(self):
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    audit = AsyncMock()
    monkeypatch.setattr(venue_codes, "record_admin_audit", audit)
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})

    result = await delete_venue(
        7,
        request=request,
        user={"id": 3, "username": "operator"},
        conn=FakeConnection(),
    )

    assert result == {"message": "场所已移除", "cloud_sync_status": "local_only"}
    assert len(statements) == 2
    assert "SELECT config_revision" in statements[0][0]
    assert "SET status='deleted'" in statements[1][0]
    assert "_venue_visits" not in statements[1][0]
    assert statements[1][1] == (5, "local_only", 3, 7)
    audit.assert_awaited_once()
