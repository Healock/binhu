import os
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from routers.environment_accounts import _temporary_password, _require_gateway_token
from services.environment_accounts import (
    EnvironmentAccountGatewayError,
    gateway_urls,
    list_accounts,
    reset_account,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    response = None
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        self.calls.append(("GET", args, kwargs))
        return self.response

    async def post(self, *args, **kwargs):
        self.calls.append(("POST", args, kwargs))
        return self.response


class EnvironmentAccountSecurityTests(unittest.TestCase):
    def test_gateway_urls_only_accepts_allowed_environments(self):
        value = '{"development":"http://gateway.internal/development","staging":"https://gateway.internal/staging"}'
        with patch.object(settings, "ENVIRONMENT_ACCOUNT_GATEWAY_URLS", value):
            self.assertEqual(gateway_urls(), {
                "development": "http://gateway.internal/development",
                "staging": "https://gateway.internal/staging",
            })

    def test_gateway_urls_reject_unknown_environment(self):
        value = '{"production":"http://gateway.internal/production"}'
        with patch.object(settings, "ENVIRONMENT_ACCOUNT_GATEWAY_URLS", value):
            with self.assertRaises(EnvironmentAccountGatewayError):
                gateway_urls()

    def test_gateway_urls_reject_cross_environment_or_unsafe_urls(self):
        for value in (
            '{"development":"http://gateway.internal/staging"}',
            '{"development":"http://user:secret@gateway.internal/development"}',
            '{"development":"http://gateway.internal/development?token=secret"}',
            '{"development":"file:///development"}',
        ):
            with self.subTest(value=value), patch.object(
                settings, "ENVIRONMENT_ACCOUNT_GATEWAY_URLS", value
            ), self.assertRaises(EnvironmentAccountGatewayError):
                gateway_urls()

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


class EnvironmentAccountGatewayResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _Client.calls = []

    def settings(self):
        return patch.multiple(
            settings,
            ENVIRONMENT_ACCOUNT_GATEWAY_URLS=(
                '{"development":"http://gateway.internal/development",'
                '"staging":"http://gateway.internal/staging"}'
            ),
            ENVIRONMENT_ACCOUNT_GATEWAY_TOKENS=(
                '{"development":"dev-token","staging":"staging-token"}'
            ),
        )

    async def test_soft_404_is_not_accepted_as_an_account_list(self):
        _Client.response = _Response({"error": "Not found", "path": "/api/internal/environment-accounts"})
        with self.settings(), patch("services.environment_accounts.httpx.AsyncClient", _Client):
            with self.assertRaises(EnvironmentAccountGatewayError):
                await list_accounts("development")

    async def test_response_environment_must_match(self):
        _Client.response = _Response({"environment": "staging", "accounts": []})
        with self.settings(), patch("services.environment_accounts.httpx.AsyncClient", _Client):
            with self.assertRaises(EnvironmentAccountGatewayError):
                await list_accounts("development")

    async def test_reset_requires_one_time_password(self):
        _Client.response = _Response({"environment": "development", "username": "observer@dev"})
        with self.settings(), patch("services.environment_accounts.httpx.AsyncClient", _Client):
            with self.assertRaises(EnvironmentAccountGatewayError):
                await reset_account("development", "observer@dev")

    async def test_environment_uses_only_its_own_url_and_token(self):
        _Client.response = _Response({"environment": "staging", "accounts": []})
        with self.settings(), patch("services.environment_accounts.httpx.AsyncClient", _Client):
            self.assertEqual(await list_accounts("staging"), [])
        method, args, kwargs = _Client.calls[-1]
        self.assertEqual(method, "GET")
        self.assertEqual(
            args[0],
            "http://gateway.internal/staging/api/internal/environment-accounts",
        )
        self.assertEqual(kwargs["headers"], {"X-Environment-Account-Token": "staging-token"})
        self.assertNotIn("dev-token", repr(_Client.calls[-1]))


if __name__ == "__main__":
    unittest.main()
