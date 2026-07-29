import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from routers.auth import UserPreferencesRequest, update_preferences
from services.theme_preferences import normalize_theme_mode


class ThemeModeTests(unittest.TestCase):
    def test_normalize_keeps_supported_modes(self):
        self.assertEqual(normalize_theme_mode("light"), "light")
        self.assertEqual(normalize_theme_mode("dark"), "dark")
        self.assertEqual(normalize_theme_mode("system"), "system")

    def test_old_or_invalid_values_fall_back_to_light(self):
        self.assertEqual(normalize_theme_mode(None), "light")
        self.assertEqual(normalize_theme_mode("unknown"), "light")

    def test_request_rejects_unknown_mode(self):
        with self.assertRaises(ValidationError):
            UserPreferencesRequest(theme_mode="sepia")


class ThemePreferenceApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_theme_mode_is_saved_to_current_account(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor_context = MagicMock()
        cursor_context.__aenter__ = AsyncMock(return_value=cursor)
        cursor_context.__aexit__ = AsyncMock(return_value=None)
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=connection)
        pool.release = MagicMock()

        with patch("routers.auth.db_manager.get_pool", return_value=pool):
            result = await update_preferences(
                UserPreferencesRequest(theme_mode="dark"),
                user={
                    "id": 7,
                    "username": "tester",
                    "role": "member",
                    "theme_mode": "light",
                },
            )

        sql, params = cursor.execute.await_args.args
        self.assertIn("theme_mode=%s", sql)
        self.assertEqual(params, ("dark", 7))
        self.assertEqual(result["user"]["theme_mode"], "dark")
        pool.release.assert_called_once_with(connection)


if __name__ == "__main__":
    unittest.main()
