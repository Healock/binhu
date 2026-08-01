import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from deps import get_current_user, require_admin, require_super_admin
from services.data_scope import filter_report_payload
from services.permissions import (
    DEFAULT_PERMISSION_GROUPS,
    ONLINE_SUMMARY_VIEW,
    SYNC_TRIGGER,
    permitted_communities,
    permitted_community,
)
from routers.sync import router as sync_router


class FakeCursor:
    def __init__(self, row, config=None, group_rows=None):
        self.row = row
        self.config = config or [("session_idle_minutes", "30"), ("permission_enforcement_enabled", "1")]
        self.group_rows = group_rows or [(
            2, "flow_post", "流口岗",
            '["online.summary.view"]', "own_department", 10,
        )]
        self.result = None
        self.updates = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=None):
        if "FROM _sessions AS session" in sql:
            self.result = self.row
        elif "SELECT config_key" in sql:
            self.result = list(self.config)
        elif "FROM _grid_members AS member" in sql and "_position_permission_group_links" in sql:
            self.result = list(self.group_rows)
        elif "FROM _user_permission_group_links AS link" in sql:
            self.result = list(self.group_rows)
        elif "FROM _permission_groups WHERE id" in sql:
            self.result = (
                2, "flow_post", "流口岗",
                '["online.summary.view"]', "own_department", 10,
            )
        elif "FROM _grid_member_department_links AS link" in sql:
            self.result = [
                (8, 5, "长板", "community", "长板"),
            ]
        elif "UPDATE _sessions SET last_activity_at" in sql:
            self.updates.append((sql, params))
            self.result = None

    async def fetchone(self):
        return self.result

    async def fetchall(self):
        return self.result or []


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class FakePool:
    def __init__(self, cursor):
        self.conn = FakeConnection(cursor)

    async def acquire(self):
        return self.conn

    def release(self, _conn):
        return None


def request(*, activity=False):
    headers = [(b"cookie", b"binhu_session=session-a")]
    if activity:
        headers.append((b"x-user-activity", b"1"))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def user_row(
    *,
    active_session="session-a",
    last_activity=None,
    now=None,
    display_name="显示姓名",
    member_name="张三",
    assignment_mode="inherited",
):
    now = now or datetime(2026, 7, 31, 8, 0, 0)
    created = now - timedelta(minutes=10)
    return (
        1, "tester", "member", "table", "three", "dock", None, "light",
        8, assignment_mode, 0, active_session, 2,
        member_name, "组员", 5, "长板", "community", "长板",
        created, last_activity or created, now + timedelta(hours=20), now,
        display_name,
    )


class PermissionDefinitionTests(unittest.IsolatedAsyncioTestCase):
    def test_sync_status_only_requires_login_but_trigger_keeps_permission(self):
        status_route = next(
            route for route in sync_router.routes
            if route.path == "/api/sync/status"
        )
        trigger_route = next(
            route for route in sync_router.routes
            if route.path == "/api/sync/trigger"
        )
        self.assertIn(
            "get_current_user",
            {dependency.call.__name__ for dependency in status_route.dependant.dependencies},
        )
        self.assertNotIn(
            "get_current_user",
            {dependency.call.__name__ for dependency in trigger_route.dependant.dependencies},
        )

    def test_position_default_groups_have_requested_boundaries(self):
        self.assertIn(ONLINE_SUMMARY_VIEW, DEFAULT_PERMISSION_GROUPS["flow_post"]["permissions"])
        self.assertNotIn(SYNC_TRIGGER, DEFAULT_PERMISSION_GROUPS["global_viewer"]["permissions"])
        self.assertIn(SYNC_TRIGGER, DEFAULT_PERMISSION_GROUPS["internal_business"]["permissions"])

    def test_own_department_requires_community_department(self):
        self.assertEqual(permitted_community({
            "data_scope": "own_department",
            "department": {"type": "community", "community_name": "长板"},
        }), "长板")
        self.assertEqual(permitted_community({
            "data_scope": "own_department",
            "department": {"type": "internal", "name": "内勤"},
        }), "")

    def test_permission_specific_scope_does_not_cross_expand(self):
        user = {
            "data_scope": "own_department",
            "permission_scopes": {
                ONLINE_SUMMARY_VIEW: "all",
                SYNC_TRIGGER: "own_department",
            },
            "department": {"type": "community", "community_name": "长板"},
        }
        self.assertIsNone(permitted_community(user, ONLINE_SUMMARY_VIEW))
        self.assertEqual(permitted_community(user, SYNC_TRIGGER), "长板")

    def test_own_department_returns_all_linked_communities(self):
        user = {
            "data_scope": "own_department",
            "departments": [
                {"type": "community", "community_name": "南厍"},
                {"type": "community", "community_name": "阅湖"},
            ],
        }
        self.assertEqual(permitted_communities(user), ["南厍", "阅湖"])

    def test_report_filter_keeps_alias_and_recalculates_later(self):
        payload = {
            "exists": True,
            "inspector": {"columns": ["社区", "数据总数"], "data": [
                {"社区": "南厍村", "数据总数": 2},
                {"社区": "长板", "数据总数": 3},
            ], "summary": {"数据总数": 5}},
        }
        user = {"data_scope": "own_department", "department": {
            "type": "community", "community_name": "南厍",
        }}
        result = filter_report_payload(payload, user, ["南厍", "南厍村"])
        self.assertEqual(result["inspector"]["data"], [{"社区": "南厍村", "数据总数": 2}])
        self.assertNotIn("summary", result["inspector"])

    async def test_legacy_dependency_calls_still_work_in_unit_tests(self):
        legacy_admin = {"id": 1, "role": "admin"}
        self.assertEqual(await require_admin(legacy_admin), legacy_admin)
        legacy_super = {"id": 2, "role": "super_admin"}
        self.assertEqual(await require_super_admin(legacy_super), legacy_super)


class SessionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_login_replaces_old_session(self):
        cursor = FakeCursor(user_row(active_session="session-b"))
        with patch("deps.db_manager.get_pool", return_value=FakePool(cursor)):
            with self.assertRaises(HTTPException) as raised:
                await get_current_user(request())
        self.assertEqual(raised.exception.detail["code"], "session_replaced")

    async def test_idle_session_expires_without_polling_refresh(self):
        now = datetime(2026, 7, 31, 8, 0, 0)
        cursor = FakeCursor(user_row(
            now=now,
            last_activity=now - timedelta(minutes=31),
        ))
        with patch("deps.db_manager.get_pool", return_value=FakePool(cursor)):
            with self.assertRaises(HTTPException) as raised:
                await get_current_user(request())
        self.assertEqual(raised.exception.detail["code"], "session_idle_timeout")
        self.assertEqual(cursor.updates, [])

    async def test_explicit_activity_refreshes_session(self):
        now = datetime(2026, 7, 31, 8, 0, 0)
        cursor = FakeCursor(user_row(
            now=now,
            last_activity=now - timedelta(minutes=20),
        ))
        with patch("deps.db_manager.get_pool", return_value=FakePool(cursor)):
            user = await get_current_user(request(activity=True))
        self.assertEqual(len(cursor.updates), 1)
        self.assertEqual(user["display_name"], "显示姓名")
        self.assertEqual(user["session_policy"]["last_activity_at"], now.isoformat() + "Z")

    async def test_display_name_falls_back_to_linked_member(self):
        cursor = FakeCursor(user_row(display_name="", member_name="关联姓名"))
        with patch("deps.db_manager.get_pool", return_value=FakePool(cursor)):
            user = await get_current_user(request())
        self.assertEqual(user["display_name"], "关联姓名")

    async def test_multiple_groups_merge_permissions_without_cross_scope(self):
        cursor = FakeCursor(
            user_row(assignment_mode="custom"),
            group_rows=[
                (
                    2, "flow_post", "流口岗",
                    '["online.summary.view", "sync.trigger"]',
                    "own_department", 10,
                ),
                (
                    3, "global_viewer", "全局查看组",
                    '["online.summary.view"]', "all", 20,
                ),
            ],
        )
        with patch("deps.db_manager.get_pool", return_value=FakePool(cursor)):
            user = await get_current_user(request())

        self.assertEqual(
            [group["code"] for group in user["permission_groups"]],
            ["flow_post", "global_viewer"],
        )
        self.assertEqual(user["permission_scopes"][ONLINE_SUMMARY_VIEW], "all")
        self.assertEqual(user["permission_scopes"][SYNC_TRIGGER], "own_department")


if __name__ == "__main__":
    unittest.main()
