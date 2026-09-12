import os
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from routers.environment_accounts import _temporary_password, _require_gateway_token
from services.environment_accounts import gateway_urls, EnvironmentAccountGatewayError


class EnvironmentAccountSecurityTests(unittest.TestCase):
    def test_gateway_urls_only_accepts_allowed_environments(self):
        with patch.object(settings, "ENVIRONMENT_ACCOUNT_GATEWAY_URLS", '{"development":"http://dev.internal","staging":"https://staging.internal","production":"http://prod"}'):
            self.assertEqual(gateway_urls(), {"development": "http://dev.internal", "staging": "https://staging.internal"})

    def test_invalid_gateway_config_fails_closed(self):
        with patch.object(settings, "ENVIRONMENT_ACCOUNT_GATEWAY_URLS", "not-json"):
            with self.assertRaises(EnvironmentAccountGatewayError):
                gateway_urls()

    def test_gateway_token_is_environment_scoped(self):
        with patch.object(settings, "APP_ENVIRONMENT", "development"), patch.object(settings, "ENVIRONMENT_ACCOUNT_GATEWAY_TOKENS", '{"development":"dev-token"}'):
            _require_gateway_token("dev-token")
            with self.assertRaises(Exception):
                _require_gateway_token("staging-token")

    def test_generated_password_is_environment_marked_and_not_reused(self):
        first = _temporary_password("development")
        second = _temporary_password("development")
        self.assertTrue(first.startswith("Dev-"))
        self.assertTrue(first.endswith("!"))
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
