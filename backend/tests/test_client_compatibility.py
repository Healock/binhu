import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from starlette.responses import Response

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import Settings, settings
from routers.app_bootstrap import get_app_bootstrap
from services.client_compatibility import (
    ClientCompatibilityMiddleware,
    compare_semver,
    evaluate_client_compatibility,
    should_check_write_version,
)


class FakeCursor:
    def __init__(self):
        self.result = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=None):
        del params
        if "config_key IN" in sql:
            self.result = [
                ("maintenance_enabled", "0"),
                ("maintenance_start_at", ""),
                ("maintenance_end_at", ""),
                ("maintenance_message", ""),
                ("timezone", "Asia/Shanghai"),
            ]
        elif "online_writeback_enabled" in sql:
            self.result = ("1",)
        elif "UTC_TIMESTAMP" in sql:
            self.result = (datetime(2026, 8, 11, 2, 30, 0),)

    async def fetchall(self):
        return self.result or []

    async def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self):
        self._cursor = FakeCursor()

    def cursor(self):
        return self._cursor


class ClientCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_minimum_versions_must_be_semver(self):
        with self.assertRaises(ValueError):
            Settings(
                MYSQL_PASSWORD="test-password",
                ENCRYPTION_KEY="test-encryption-key",
                WINDOWS_MIN_SUPPORTED_VERSION="latest",
            )

    def test_semver_comparison_includes_prerelease_rules(self):
        self.assertEqual(compare_semver("1.2.3", "1.2.3"), 0)
        self.assertEqual(compare_semver("1.2.4", "1.2.3"), 1)
        self.assertEqual(compare_semver("1.2.3-beta.2", "1.2.3-beta.10"), -1)
        self.assertEqual(compare_semver("1.2.3", "1.2.3-rc.1"), 1)
        self.assertIsNone(compare_semver("latest", "1.2.3"))

    def test_old_or_invalid_supported_client_requires_upgrade(self):
        minimum = {"windows": "2.0.0", "android": "3.0.0"}
        old = evaluate_client_compatibility(
            "Windows",
            "1.9.9",
            minimum_versions=minimum,
            enforcement_enabled=True,
        )
        self.assertTrue(old["must_upgrade"])
        self.assertTrue(old["write_blocked"])
        self.assertEqual(old["reason"], "version_too_old")

        invalid = evaluate_client_compatibility(
            "android",
            "latest",
            minimum_versions=minimum,
            enforcement_enabled=True,
        )
        self.assertEqual(invalid["reason"], "invalid_version")

    def test_web_requests_are_not_treated_as_native_clients(self):
        compatibility = evaluate_client_compatibility(
            None,
            None,
            minimum_versions={"windows": "2.0.0", "android": "3.0.0"},
        )
        self.assertFalse(compatibility["supported_platform"])
        self.assertFalse(compatibility["must_upgrade"])
        self.assertTrue(should_check_write_version("POST", "/api/tasks"))
        self.assertFalse(should_check_write_version("GET", "/api/tasks"))
        self.assertFalse(should_check_write_version("POST", "/api/auth/logout"))

    def test_identification_requirement_blocks_headerless_writes(self):
        missing = evaluate_client_compatibility(
            None,
            None,
            minimum_versions={"windows": "2.0.0", "android": "3.0.0"},
            enforcement_enabled=True,
            identification_required=True,
        )
        self.assertTrue(missing["write_blocked"])
        self.assertEqual(missing["reason"], "missing_platform")

        web = evaluate_client_compatibility(
            "web",
            "0.19.2",
            minimum_versions={"windows": "2.0.0", "android": "3.0.0"},
            enforcement_enabled=True,
            identification_required=True,
        )
        self.assertTrue(web["supported_platform"])
        self.assertFalse(web["native_version_policy"])
        self.assertFalse(web["write_blocked"])

    async def test_middleware_returns_426_for_old_native_client(self):
        downstream_called = False

        async def downstream(scope, receive, send):
            del scope, receive, send
            nonlocal downstream_called
            downstream_called = True

        middleware = ClientCompatibilityMiddleware(downstream)
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/work-logs/drafts",
            "headers": [
                (b"x-binhu-client-platform", b"windows"),
                (b"x-binhu-client-version", b"1.0.0"),
            ],
        }
        with (
            patch.object(settings, "WINDOWS_MIN_SUPPORTED_VERSION", "2.0.0"),
            patch.object(settings, "CLIENT_WRITE_VERSION_ENFORCEMENT_ENABLED", True),
        ):
            await middleware(scope, receive, send)

        self.assertFalse(downstream_called)
        self.assertEqual(messages[0]["status"], 426)
        body = json.loads(messages[1]["body"])
        self.assertEqual(body["detail"]["code"], "client_upgrade_required")

    async def test_bootstrap_returns_business_context_and_account_features(self):
        response = Response()
        with (
            patch.object(settings, "WINDOWS_MIN_SUPPORTED_VERSION", "2.0.0"),
            patch.object(settings, "ANDROID_MIN_SUPPORTED_VERSION", "3.0.0"),
            patch.object(settings, "REGISTRY_FEATURE_ENABLED", True),
            patch.object(settings, "WORKFLOW_FEATURE_ENABLED", False),
        ):
            payload = await get_app_bootstrap(
                response=response,
                client_platform="windows",
                client_version="1.9.0",
                user={"permissions": ["online.raw.view", "online.raw.edit"]},
                conn=FakeConnection(),
            )

        self.assertEqual(payload["server_version"], "0.22.6")
        self.assertEqual(payload["business_date"], "2026-08-11")
        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertTrue(payload["must_upgrade"])
        self.assertTrue(payload["authenticated"])
        self.assertEqual(
            payload["available_features"],
            ["online.raw.edit", "online.raw.view"],
        )
        self.assertTrue(payload["feature_flags"]["registry"])
        self.assertTrue(payload["feature_flags"]["online_writeback"])
        self.assertFalse(
            payload["feature_flags"]["client_write_identification_required"]
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
