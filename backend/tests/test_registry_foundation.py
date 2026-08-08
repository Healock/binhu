from __future__ import annotations

import os

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from services.permissions import (
    ALL_PERMISSIONS,
    DEFAULT_PERMISSION_GROUPS,
    REGISTRY_PROPERTY_MANAGE,
    REGISTRY_PROPERTY_VIEW,
    REGISTRY_WATCH_MANAGE,
    WORKFLOW_TICKET_CREATE,
)
from services.registry_security import hmac_digest, normalize_identity, normalize_phone


def test_registry_normalization_and_hmac_are_deterministic():
    assert normalize_identity(" 320000 19990101999x ") == "32000019990101999X"
    assert normalize_phone("138-0000-0000") == "13800000000"
    first, version = hmac_digest(" 320000 19990101999x ", kind="identity")
    second, second_version = hmac_digest("32000019990101999X", kind="identity")
    assert first and first == second
    assert version == second_version == 1
    assert "32000019990101999" not in first


def test_empty_sensitive_values_do_not_create_digest():
    assert hmac_digest("", kind="identity") == (None, 1)
    assert hmac_digest("  ", kind="phone") == (None, 1)


def test_new_permissions_are_catalogued_and_defaulted():
    assert {REGISTRY_PROPERTY_VIEW, REGISTRY_PROPERTY_MANAGE, REGISTRY_WATCH_MANAGE,
            WORKFLOW_TICKET_CREATE}.issubset(ALL_PERMISSIONS)
    assert REGISTRY_PROPERTY_VIEW in DEFAULT_PERMISSION_GROUPS["internal_business"]["permissions"]
    assert WORKFLOW_TICKET_CREATE in DEFAULT_PERMISSION_GROUPS["flow_post"]["permissions"]
    assert settings.registry_hmac_key
