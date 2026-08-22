import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, Response

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import Settings, settings
from database import ensure_bootstrap_admin
from deps import get_session_cookie_config, require_super_admin
from http_security import add_cors_middleware
from routers.system import router as system_router


class BootstrapCursor:
    def __init__(self, user_count=0):
        self.user_count = user_count
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return (self.user_count,)


class SecurityFoundationTests(unittest.IsolatedAsyncioTestCase):
    def test_cors_rejects_wildcard_origin(self):
        with patch.object(settings, "CORS_ALLOWED_ORIGINS", "*"):
            with self.assertRaises(ValueError):
                _ = settings.cors_allowed_origins

    def test_cookie_security_comes_from_settings(self):
        with patch("deps.settings.SESSION_COOKIE_SECURE", True), patch(
            "deps.settings.SESSION_COOKIE_SAMESITE",
            "none",
        ):
            config = get_session_cookie_config()

        self.assertTrue(config["secure"])
        self.assertTrue(config["httponly"])
        self.assertEqual(config["samesite"], "none")
        self.assertEqual(config["path"], "/")

        response = Response()
        response.set_cookie(value="test-session", **config)
        cookie_header = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie_header)
        self.assertIn("secure", cookie_header)
        self.assertIn("samesite=none", cookie_header)
        self.assertIn("path=/", cookie_header)

    def test_cross_site_cookie_requires_secure(self):
        with self.assertRaisesRegex(ValueError, "SameSite=None"):
            Settings(
                MYSQL_PASSWORD="test",
                ENCRYPTION_KEY="test-key",
                OPS_AGENT_TOKEN="test-token",
                SESSION_COOKIE_SECURE=False,
                SESSION_COOKIE_SAMESITE="none",
            )

    async def test_desktop_client_preflight_allows_explicit_origins(self):
        desktop_origins = [
            "http://tauri.localhost",
            "https://tauri.localhost",
            "binhu://app",
        ]
        app = FastAPI()
        add_cors_middleware(app, desktop_origins)

        @app.post("/api/auth/login")
        async def login_probe():
            return {"ok": True}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://api.example.test",
        ) as client:
            for origin in desktop_origins:
                response = await client.options(
                    "/api/auth/login",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": (
                            "content-type,x-binhu-client-platform,"
                            "x-binhu-client-version,x-binhu-device-id"
                        ),
                    },
                )

                self.assertEqual(response.status_code, 200, origin)
                self.assertEqual(
                    response.headers.get("access-control-allow-origin"),
                    origin,
                )
                self.assertEqual(
                    response.headers.get("access-control-allow-credentials"),
                    "true",
                )
                allowed_headers = response.headers.get(
                    "access-control-allow-headers",
                    "",
                ).lower()
                self.assertIn("x-binhu-device-id", allowed_headers)

    async def test_desktop_client_preflight_rejects_unknown_origin(self):
        app = FastAPI()
        add_cors_middleware(app, ["https://tauri.localhost"])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://api.example.test",
        ) as client:
            response = await client.options(
                "/api/auth/login",
                headers={
                    "Origin": "https://untrusted.example",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "x-binhu-device-id",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_system_routes_require_super_admin(self):
        for route in system_router.routes:
            self.assertTrue(
                any(
                    dependency.call is require_super_admin
                    for dependency in route.dependant.dependencies
                )
            )

    async def test_empty_user_table_rejects_missing_bootstrap_credentials(self):
        cursor = BootstrapCursor()
        with patch("database.settings.BOOTSTRAP_ADMIN_USERNAME", ""), patch(
            "database.settings.BOOTSTRAP_ADMIN_PASSWORD",
            "",
        ):
            with self.assertRaises(RuntimeError):
                await ensure_bootstrap_admin(cursor)

        self.assertFalse(
            any(sql.startswith("INSERT INTO _users") for sql, _ in cursor.executed)
        )

    async def test_bootstrap_password_is_hashed_and_never_printed(self):
        cursor = BootstrapCursor()
        with patch(
            "database.settings.BOOTSTRAP_ADMIN_USERNAME",
            "first-admin",
        ), patch(
            "database.settings.BOOTSTRAP_ADMIN_PASSWORD",
            "one-time-secret",
        ), patch("builtins.print") as mocked_print:
            created = await ensure_bootstrap_admin(cursor)

        self.assertTrue(created)
        insert = next(
            params
            for sql, params in cursor.executed
            if sql.startswith("INSERT INTO _users")
        )
        self.assertEqual(insert[0], "first-admin")
        self.assertNotEqual(insert[1], "one-time-secret")
        self.assertNotIn("one-time-secret", str(mocked_print.call_args_list))

    async def test_existing_users_do_not_require_bootstrap_values(self):
        cursor = BootstrapCursor(user_count=1)
        with patch("database.settings.BOOTSTRAP_ADMIN_USERNAME", ""), patch(
            "database.settings.BOOTSTRAP_ADMIN_PASSWORD",
            "",
        ):
            self.assertFalse(await ensure_bootstrap_admin(cursor))


if __name__ == "__main__":
    unittest.main()
