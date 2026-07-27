from datetime import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from routers.notifications import (
    get_unread_count,
    list_notifications,
    mark_all_read,
    mark_read,
)
from services.notifications import create_sync_failure_notifications


class NotificationCursor:
    def __init__(self):
        self.executed = []
        self.last_sql = ""
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        self.last_sql = " ".join(sql.split())
        self.executed.append((self.last_sql, params))

    async def fetchone(self):
        if self.last_sql.startswith("SELECT COUNT(*) FROM _notifications"):
            return (1,)
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

    async def test_notification_list_is_scoped_to_current_user(self):
        cursor = NotificationCursor()
        result = await list_notifications(
            limit=20,
            user={"id": 5, "role": "super_admin"},
            conn=make_connection(cursor),
        )

        self.assertEqual(result["unread_count"], 1)
        self.assertEqual(result["data"][0]["id"], 9)
        for sql, params in cursor.executed:
            self.assertIn(5, params)

    async def test_unread_count_is_scoped_to_current_user(self):
        cursor = NotificationCursor()
        result = await get_unread_count(
            user={"id": 5, "role": "super_admin"},
            conn=make_connection(cursor),
        )

        self.assertEqual(result, {"unread_count": 1})
        self.assertEqual(cursor.executed[0][1], (5,))

    async def test_mark_one_and_all_read_are_scoped_to_current_user(self):
        cursor = NotificationCursor()
        connection = make_connection(cursor)
        user = {"id": 5, "role": "super_admin"}

        await mark_read(9, user=user, conn=connection)
        await mark_all_read(user=user, conn=connection)

        one_sql, one_params = cursor.executed[0]
        all_sql, all_params = cursor.executed[1]
        self.assertIn("WHERE id=%s AND user_id=%s", one_sql)
        self.assertEqual(one_params, (9, 5))
        self.assertIn("WHERE user_id=%s AND is_read=0", all_sql)
        self.assertEqual(all_params, (5,))


if __name__ == "__main__":
    unittest.main()
