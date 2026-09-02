import os
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import Settings, settings
from services.environment_identity import is_shadow_username, production_username_allowed


class EnvironmentIdentityTests(unittest.TestCase):
    def test_environment_enum_is_strict(self):
        configured = Settings(
            MYSQL_PASSWORD="test-password",
            ENCRYPTION_KEY="test-encryption-key",
            APP_ENVIRONMENT="SHADOW",
            SESSION_COOKIE_NAME="binhu_shadow_session",
            LOAD_TEST_RUN_ID="LT-20260902-01",
        )
        self.assertEqual(configured.APP_ENVIRONMENT, "shadow")
        with self.assertRaises(ValueError):
            Settings(
                MYSQL_PASSWORD="test-password",
                ENCRYPTION_KEY="test-encryption-key",
                APP_ENVIRONMENT="staging",
            )

    def test_shadow_environment_requires_its_cookie(self):
        with self.assertRaises(ValueError):
            Settings(
                MYSQL_PASSWORD="test-password",
                ENCRYPTION_KEY="test-encryption-key",
                APP_ENVIRONMENT="shadow",
                SESSION_COOKIE_NAME="binhu_session",
                LOAD_TEST_RUN_ID="LT-20260902-01",
            )

    def test_shadow_environment_requires_a_run_id(self):
        with self.assertRaises(ValueError):
            Settings(
                MYSQL_PASSWORD="test-password",
                ENCRYPTION_KEY="test-encryption-key",
                APP_ENVIRONMENT="shadow",
                SESSION_COOKIE_NAME="binhu_shadow_session",
                LOAD_TEST_RUN_ID="",
            )

    def test_production_rejects_reserved_shadow_suffix(self):
        self.assertTrue(is_shadow_username(" Observer@Shadow "))
        with patch.object(settings, "APP_ENVIRONMENT", "production"):
            self.assertFalse(production_username_allowed("observer@shadow"))
            self.assertTrue(production_username_allowed("observer"))
        with patch.object(settings, "APP_ENVIRONMENT", "shadow"):
            self.assertTrue(production_username_allowed("observer@shadow"))


if __name__ == "__main__":
    unittest.main()
