from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from routers.notifications import (
    AnnouncementCreate,
    create_announcement,
    delete_announcement,
    get_unread_count,
    list_notifications,
    mark_all_read,
    mark_announcement_read,
    mark_read,
    router,
)
from services.notifications import create_sync_failure_notifications


class NotificationCursor:
    def __init__(self):
        self.executed = []
        self.last_sql = ""
        self.rowcount = 1
        self.lastrowid = 14

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        self.last_sql = " ".join(sql.split())
        self.executed.append((self.last_sql, params))
        self.rowcount = 1

    async def fetchone(self):
        if self.last_sql.startswith("SELECT COUNT(*) FROM _notifications"):
            return (1,)
        if self.last_sql.startswith("SELECT COUNT(*) FROM _announcements"):
            return (2,)
        if self.last_sql.startswith("SELECT id FROM _announcements"):
            return (4,)
        if self.last_sql.startswith("SELECT id FROM _notifications"):
            return (9,)
        return None

    async def fetchall(self):
        if self.last_sql.startswith("SELECT id, category, severity"):
            return [
                (
                    9,
                    "sync",
                    "error",
                    "Automatic sync failed",
                    "Task #12 failed",
                    12,
                    0,
                    datetime(2026, 7, 27, 8, 0, 0),
                    None,
                )
            ]
        if self.last_sql.startswith("SELECT a.id, a.severity"):
            return [
                (
                    4,
                    "warning",
                    "Attendance history notice",
                    "History starts on 2026-07-30",
                    0,
                    datetime(2026, 7, 30, 8, 0, 0),
                    None,
                )
            ]
        return []


def make_connection(cursor):
    connection = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=cursor)
    context.__aexit__ = AsyncMock(return_value=None)
    connection.cursor.return_value = context
    return connection


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_notice_targets_every_super_admin(self):
        cursor = NotificationCursor()
        connection = make_connection(cursor)
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=connection)
        pool.release = MagicMock()

        with patch(
            "services.notifications.db_manager.get_pool",
            return_value=pool,
        ):
            await create_sync_failure_notifications(
                12,
                "failed",
                "network unavailable",
            )

        sql, params = cursor.executed[0]
        self.assertIn("INSERT IGNORE INTO _notifications", sql)
        self.assertIn("FROM _users", sql)
        self.assertIn("WHERE role = 'super_admin'", sql)
        self.assertEqual(params[0], "error")
        self.assertEqual(params[-1], 12)

    async def test_partial_notice_uses_warning_severity(self):
        cursor = NotificationCursor()
        connection = make_connection(cursor)
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=connection)
        pool.release = MagicMock()

        with patch(
            "services.notifications.db_manager.get_pool",
            return_value=pool,
        ):
            await create_sync_failure_notifications(
                13,
                "partial",
                "one sheet failed",
            )

        self.assertEqual(cursor.executed[0][1][0], "warning")

    async def test_regular_user_receives_announcements_and_personal_messages(self):
        cursor = NotificationCursor()
        result = await list_notifications(
            limit=20,
            user={"id": 5, "role": "member"},
            conn=make_connection(cursor),
        )

        self.assertEqual(result["unread_count"], 3)
        self.assertEqual(result["personal_unread_count"], 1)
        self.assertEqual(result["announcement_unread_count"], 2)
        self.assertEqual(
            {item["source"] for item in result["data"]},
            {"personal", "announcement"},
        )
        for _, params in cursor.executed:
            self.assertIn(5, params)

    async def test_unread_count_combines_both_message_types(self):
        cursor = NotificationCursor()
        result = await get_unread_count(
            user={"id": 5, "role": "member"},
            conn=make_connection(cursor),
        )

        self.assertEqual(
            result,
            {
                "unread_count": 3,
                "personal_unread_count": 1,
                "announcement_unread_count": 2,
            },
        )

    async def test_read_operations_are_scoped_to_current_user(self):
        cursor = NotificationCursor()
        connection = make_connection(cursor)
        user = {"id": 5, "role": "member"}

        await mark_read(9, user=user, conn=connection)
        await mark_announcement_read(4, user=user, conn=connection)
        await mark_all_read(user=user, conn=connection)

        personal_sql, personal_params = cursor.executed[0]
        announcement_sql, announcement_params = cursor.executed[1]
        all_personal_sql, all_personal_params = cursor.executed[2]
        all_announcement_sql, all_announcement_params = cursor.executed[3]
        self.assertIn("WHERE id=%s AND user_id=%s", personal_sql)
        self.assertEqual(personal_params, (9, 5))
        self.assertIn("INSERT IGNORE INTO _announcement_reads", announcement_sql)
        self.assertEqual(announcement_params, (5, 4))
        self.assertEqual(all_personal_params, (5,))
        self.assertIn("INSERT IGNORE INTO _announcement_reads", all_announcement_sql)
        self.assertEqual(all_announcement_params, (5,))

    async def test_super_admin_can_publish_and_delete_announcement(self):
        cursor = NotificationCursor()
        connection = make_connection(cursor)
        request = SimpleNamespace(headers={}, client=None)
        user = {"id": 1, "username": "root", "role": "super_admin"}
        audit = AsyncMock()
        with patch("routers.notifications.record_admin_audit", new=audit):
            created = await create_announcement(
                AnnouncementCreate(
                    title="  Data notice  ",
                    content="  New rule  ",
                    severity="warning",
                ),
                request,
                user=user,
                conn=connection,
            )
            deleted = await delete_announcement(
                14,
                request,
                user=user,
                conn=connection,
            )

        self.assertEqual(created["id"], 14)
        self.assertEqual(deleted["message"], "公告已删除")
        self.assertEqual(audit.await_count, 2)
        create_params = cursor.executed[0][1]
        self.assertEqual(create_params[1:3], ("Data notice", "New rule"))

    def test_message_routes_use_login_or_super_admin_permissions(self):
        for route in router.routes:
            dependency_names = {
                dependency.call.__name__
                for dependency in route.dependant.dependencies
                if dependency.call
            }
            if route.path == "/api/notifications/announcements" or (
                route.path == "/api/notifications/announcements/{announcement_id}"
                and "DELETE" in route.methods
            ):
                self.assertIn("require_announcement_manage", dependency_names)
            else:
                self.assertIn("require_notification_view", dependency_names)


if __name__ == "__main__":
    unittest.main()
