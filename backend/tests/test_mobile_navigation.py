import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from routers.auth import UserPreferencesRequest, update_preferences
from services.mobile_navigation import (
    default_mobile_dock_config,
    normalize_mobile_dock_config,
    normalize_mobile_navigation_mode,
    validate_mobile_dock_config,
)


class MobileNavigationConfigTests(unittest.TestCase):
    def test_navigation_mode_defaults_to_dock(self):
        self.assertEqual(normalize_mobile_navigation_mode(None), "dock")
        self.assertEqual(normalize_mobile_navigation_mode("unknown"), "dock")
        self.assertEqual(normalize_mobile_navigation_mode("sidebar"), "sidebar")

    def test_default_config_follows_role_permissions(self):
        member = default_mobile_dock_config("member")
        admin = default_mobile_dock_config("admin")
        super_admin = default_mobile_dock_config("super_admin")

        member_items = {
            item
            for group in member["groups"]
            for item in group["items"]
        }
        super_items = {
            item
            for group in super_admin["groups"]
            for item in group["items"]
        }
        self.assertNotIn("users", member_items)
        self.assertNotIn("operations", member_items)
        self.assertNotIn("data_upload", member_items)
        self.assertNotIn("work_log", member_items)
        admin_items = {
            item
            for group in admin["groups"]
            for item in group["items"]
        }
        self.assertIn("data_upload", admin_items)
        self.assertIn("work_log", admin_items)
        self.assertNotIn("users", admin_items)
        self.assertIn("users", super_items)
        self.assertIn("operations", super_items)
        self.assertIn("data_upload", super_items)
        self.assertIn("work_log", super_items)

    def test_normalize_filters_unknown_duplicate_and_forbidden_items(self):
        result = normalize_mobile_dock_config(
            {
                "groups": [
                    {
                        "id": "resources",
                        "items": [
                            "grid_members",
                            "users",
                            "grid_members",
                        ],
                    },
                    {
                        "id": "resources",
                        "items": ["communities"],
                    },
                    {
                        "id": "unknown",
                        "items": ["unknown"],
                    },
                ],
            },
            "member",
        )

        self.assertEqual(
            result,
            {
                "groups": [
                    {
                        "id": "resources",
                        "items": ["grid_members"],
                    },
                ],
            },
        )

    def test_normalize_restores_default_when_nothing_is_accessible(self):
        result = normalize_mobile_dock_config(
            {
                "groups": [
                    {
                        "id": "resources",
                        "items": ["users"],
                    },
                ],
            },
            "member",
        )
        self.assertEqual(result, default_mobile_dock_config("member"))

    def test_permission_list_overrides_legacy_role_navigation(self):
        permissions = [
            "online.summary.view",
            "visit.import",
            "worklog.manage",
        ]
        config = default_mobile_dock_config("member", permissions)
        items = {
            item
            for group in config["groups"]
            for item in group["items"]
        }

        self.assertIn("online_summary", items)
        self.assertIn("data_upload", items)
        self.assertIn("work_log", items)
        self.assertNotIn("online_query", items)
        self.assertNotIn("users", items)

        with self.assertRaisesRegex(ValueError, "无权访问"):
            validate_mobile_dock_config(
                {
                    "groups": [
                        {"id": "resources", "items": ["users"]},
                    ],
                },
                "super_admin",
                permissions,
            )

    def test_strict_validation_rejects_duplicates_and_forbidden_items(self):
        with self.assertRaisesRegex(ValueError, "分类不能重复"):
            validate_mobile_dock_config(
                {
                    "groups": [
                        {"id": "workspace", "items": ["online_summary"]},
                        {"id": "workspace", "items": ["online_query"]},
                    ],
                },
                "member",
            )

        with self.assertRaisesRegex(ValueError, "无权访问"):
            validate_mobile_dock_config(
                {
                    "groups": [
                        {"id": "system", "items": ["operations"]},
                    ],
                },
                "member",
            )

    def test_strict_validation_rejects_group_count_and_empty_group(self):
        with self.assertRaisesRegex(ValueError, "1 至 4"):
            validate_mobile_dock_config(
                {
                    "groups": [
                        {"id": "workspace", "items": ["online_summary"]},
                    ]
                    * 5,
                },
                "member",
            )

        with self.assertRaisesRegex(ValueError, "至少保留一个页面"):
            validate_mobile_dock_config(
                {
                    "groups": [
                        {"id": "workspace", "items": []},
                    ],
                },
                "member",
            )


class MobileNavigationPreferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_navigation_preference_is_saved(self):
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
        dock_config = {
            "groups": [
                {
                    "id": "workspace",
                    "items": ["visit_summary", "online_summary"],
                },
            ],
        }

        with patch("routers.auth.db_manager.get_pool", return_value=pool):
            result = await update_preferences(
                UserPreferencesRequest(
                    mobile_navigation_mode="sidebar",
                    mobile_dock_config=dock_config,
                ),
                user={
                    "id": 7,
                    "username": "tester",
                    "role": "member",
                    "table_display_mode": "table",
                    "report_column_mode": "three",
                    "mobile_navigation_mode": "dock",
                    "mobile_dock_config": default_mobile_dock_config(
                        "member",
                    ),
                },
            )

        sql, params = cursor.execute.await_args.args
        self.assertIn("mobile_navigation_mode=%s", sql)
        self.assertIn("mobile_dock_config=%s", sql)
        self.assertEqual(params[0], "sidebar")
        self.assertEqual(json.loads(params[1]), dock_config)
        self.assertEqual(params[2], 7)
        self.assertEqual(
            result["user"]["mobile_navigation_mode"],
            "sidebar",
        )
        self.assertEqual(result["user"]["mobile_dock_config"], dock_config)
        pool.release.assert_called_once_with(connection)

    async def test_invalid_config_is_rejected_before_database_acquire(self):
        with patch("routers.auth.db_manager.get_pool") as get_pool:
            with self.assertRaises(HTTPException) as raised:
                await update_preferences(
                    UserPreferencesRequest(
                        mobile_dock_config={
                            "groups": [
                                {
                                    "id": "system",
                                    "items": ["operations"],
                                },
                            ],
                        },
                    ),
                    user={
                        "id": 8,
                        "username": "member",
                        "role": "member",
                    },
                )

        self.assertEqual(raised.exception.status_code, 422)
        get_pool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
