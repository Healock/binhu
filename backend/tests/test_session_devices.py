import os
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.session_devices import (
    hash_device_id,
    infer_device_type,
    user_agent_family,
)


class SessionDeviceTests(unittest.TestCase):
    def test_mobile_hint_wins_over_conflicting_client_declarations(self):
        self.assertEqual(
            infer_device_type(
                requested="desktop",
                platform_header="desktop",
                mobile_hint="?1",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            ),
            "mobile",
        )

    def test_unknown_device_type_safely_defaults_to_desktop(self):
        self.assertEqual(infer_device_type(requested="watch"), "desktop")

    def test_device_hash_is_keyed_and_does_not_store_raw_value(self):
        raw = "browser-device-1234567890"
        with patch("services.session_devices.settings.ENCRYPTION_KEY", "test-key"):
            digest = hash_device_id(raw)
        self.assertIsNotNone(digest)
        self.assertNotEqual(digest, raw)
        self.assertEqual(len(digest), 64)
        self.assertIsNone(hash_device_id(""))
        self.assertIsNone(hash_device_id("x" * 129))

    def test_user_agent_is_reduced_to_safe_family(self):
        self.assertEqual(
            user_agent_family(
                "Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36"
            ),
            "Chrome",
        )
        self.assertEqual(user_agent_family(""), "其他浏览器")


if __name__ == "__main__":
    unittest.main()
