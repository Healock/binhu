import unittest
from unittest.mock import AsyncMock, patch

from config import settings
from database import ensure_bootstrap_admin
from deps import get_session_cookie_config, require_super_admin
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
            "lax",
        ):
            config = get_session_cookie_config()

        self.assertTrue(config["secure"])
        self.assertTrue(config["httponly"])
        self.assertEqual(config["samesite"], "lax")
        self.assertEqual(config["path"], "/")

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
