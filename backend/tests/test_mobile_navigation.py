import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from deps import require_admin_account
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
        self.assertIn("flow_tasks", admin_items)
        self.assertNotIn("users", admin_items)
        self.assertIn("users", super_items)
        # 设置是第五组，默认 Dock 最多四组；用户可在个性化设置中替换进入。
        self.assertNotIn("operations", super_items)
        self.assertIn("data_upload", super_items)
        self.assertIn("work_log", super_items)
        self.assertIn("flow_tasks", super_items)

    def test_admin_permission_group_exposes_dedicated_flow_tasks(self):
        permissions = ["online.raw.view"]
        member = default_mobile_dock_config("member", permissions)
        delegated_admin = default_mobile_dock_config(
            "member", permissions, ["admin"]
        )
        self.assertNotIn(
            "flow_tasks",
            {item for group in member["groups"] for item in group["items"]},
        )
        self.assertIn(
            "flow_tasks",
            {
                item
                for group in delegated_admin["groups"]
                for item in group["items"]
            },
        )
        self.assertNotIn(
            "online_query",
            {item for group in member["groups"] for item in group["items"]},
        )
        self.assertIn(
            "online_query",
            {
                item
                for group in delegated_admin["groups"]
                for item in group["items"]
            },
        )
    def test_normalize_filters_unknown_duplicate_and_forbidden_items(self):
        result = normalize_mobile_dock_config(
            {
                "version": 2,
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
                "version": 2,
                "groups": [
                    {"id": "workspace", "items": ["dashboard"]},
                    {
                        "id": "resources",
                        "items": ["grid_members"],
                    },
                    {
                        "id": "summaries",
                        "items": ["online_summary", "visit_summary"],
                    },
                    {
                        "id": "system",
                        "items": ["settings", "workflow_config"],
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
                    "version": 2,
                    "groups": [
                        {"id": "resources", "items": ["users"]},
                    ],
                },
                "super_admin",
                permissions,
            )

    def test_police_permissions_expose_upload_and_address_pages(self):
        config = default_mobile_dock_config(
            "member",
            ["police.dispatch.manage", "police.address.manage"],
        )
        items = {
            item
            for group in config["groups"]
            for item in group["items"]
        }
        self.assertIn("data_upload", items)
        self.assertIn("police_addresses", items)

    def test_old_task_items_move_to_new_navigation_pages(self):
        config = normalize_mobile_dock_config(
            {
                "version": 2,
                "groups": [
                    {"id": "workspace", "items": ["dashboard"]},
                    {
                        "id": "tasks",
                        "items": ["police_tasks", "workflow_tickets"],
                    },
                ],
            },
            "member",
            [
                "police.dispatch.manage",
                "workflow.ticket.view",
                "workflow.ticket.handle",
            ],
            position="基础管控",
        )
        groups = {group["id"]: group["items"] for group in config["groups"]}
        self.assertEqual(
            groups["workspace"],
            ["dashboard", "workflow_tickets"],
        )
        self.assertEqual(
            groups["tasks"],
            ["police_tasks", "police_analysis", "photo_tasks"],
        )

    def test_strict_validation_rejects_duplicates_and_forbidden_items(self):
        with self.assertRaisesRegex(ValueError, "分类不能重复"):
            validate_mobile_dock_config(
                {
                    "version": 2,
                    "groups": [
                        {"id": "workspace", "items": ["dashboard"]},
                        {"id": "workspace", "items": ["online_query"]},
                    ],
                },
                "member",
            )

        with self.assertRaisesRegex(ValueError, "无权访问"):
            validate_mobile_dock_config(
                {
                    "version": 2,
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
                    "version": 2,
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
                    "version": 2,
                    "groups": [
                        {"id": "workspace", "items": []},
                    ],
                },
                "member",
            )


class AdminQueryAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_view_permission_alone_does_not_grant_query_access(self):
        with self.assertRaises(HTTPException) as raised:
            await require_admin_account({
                "role": "member",
                "permissions": ["online.raw.view"],
                "permission_groups": [{"code": "flow_post"}],
            })

        self.assertEqual(raised.exception.status_code, 403)

    async def test_admin_permission_group_grants_query_access(self):
        user = {
            "role": "member",
            "permission_groups": [{"code": "admin"}],
        }

        self.assertIs(await require_admin_account(user), user)


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
            "version": 2,
            "groups": [
                {
                    "id": "summaries",
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
        expected_dock_config = {
            "version": 2,
            "groups": [{
                "id": "workspace",
                "items": ["dashboard"],
            }, {
                "id": "summaries",
                "items": ["visit_summary", "online_summary"],
            }, {
                "id": "resources",
                "items": [
                    "grid_members",
                    "communities",
                    "registry",
                    "watch_people",
                ],
            }, {
                "id": "system",
                "items": ["settings", "workflow_config"],
            }],
        }
        self.assertEqual(json.loads(params[1]), expected_dock_config)
        self.assertEqual(params[2], 7)
        self.assertEqual(
            result["user"]["mobile_navigation_mode"],
            "sidebar",
        )
        self.assertEqual(
            result["user"]["mobile_dock_config"], expected_dock_config
        )
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
