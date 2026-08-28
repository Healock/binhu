import os
import sys
from pathlib import Path

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers.venue_codes import _check_form_token, _form_token, _token_digest, _validate_photo


def test_venue_token_is_signed_and_expires():
    token = _form_token(12, 1_800_000_000)
    assert _check_form_token(token, 12) is False
    fresh = _form_token(12, __import__("time").time().__floor__())
    assert _check_form_token(fresh, 12)
    assert _token_digest("abc") != _token_digest("def")


def test_photo_magic_validation():
    mime, size, digest = _validate_photo("a.jpg", "image/jpeg", b"\xff\xd8\xff" + b"x")
    assert mime == "image/jpeg" and size == 4 and len(digest) == 64
