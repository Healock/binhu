from datetime import date
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.grid_members import GridMemberLeaveUpdate, update_member_leave


class LeaveCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        self.connection.calls.append((" ".join(sql.split()), params))
        self.rowcount = 1

    async def fetchone(self):
        return (1,)


class LeaveConnection:
    def __init__(self):
        self.calls = []
        self.began = False
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return LeaveCursor(self)

    async def begin(self):
        self.began = True

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class GridMemberLeaveHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_leave_is_written_to_history_and_compatibility_fields(self):
        connection = LeaveConnection()
        with patch(
            "routers.grid_members.get_business_date",
            new=AsyncMock(return_value=date(2026, 7, 30)),
        ):
            await update_member_leave(
                1,
                GridMemberLeaveUpdate(
                    action="temporary",
                    leave_start_date=date(2026, 7, 30),
                    leave_end_date=date(2026, 8, 1),
                    leave_reason="年假",
                ),
                user={"id": 9},
                conn=connection,
            )

        sql = "\n".join(call[0] for call in connection.calls)
        self.assertIn("INSERT INTO _personnel_attendance_history", sql)
        self.assertIn("UPDATE _grid_members", sql)
        self.assertTrue(connection.began)
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)

    async def test_past_leave_backfill_does_not_change_current_status(self):
        connection = LeaveConnection()
        with patch(
            "routers.grid_members.get_business_date",
            new=AsyncMock(return_value=date(2026, 7, 30)),
        ):
            result = await update_member_leave(
                1,
                GridMemberLeaveUpdate(
                    action="temporary",
                    leave_start_date=date(2026, 7, 1),
                    leave_end_date=date(2026, 7, 2),
                    leave_reason="补录",
                ),
                user={"id": 9},
                conn=connection,
            )

        sql = "\n".join(call[0] for call in connection.calls)
        self.assertIn("INSERT INTO _personnel_attendance_history", sql)
        self.assertNotIn("UPDATE _grid_members", sql)
        self.assertNotIn("SET is_active=0", sql)
        self.assertEqual(result["message"], "过去的请假记录已补录")

    async def test_clear_closes_open_history_and_clears_compatibility_fields(self):
        connection = LeaveConnection()
        with patch(
            "routers.grid_members.get_business_date",
            new=AsyncMock(return_value=date(2026, 7, 30)),
        ):
            await update_member_leave(
                1,
                GridMemberLeaveUpdate(action="clear"),
                user={"id": 9},
                conn=connection,
            )

        sql = "\n".join(call[0] for call in connection.calls)
        self.assertIn("SET is_active=0", sql)
        self.assertIn("SET end_date=%s", sql)
        self.assertIn("UPDATE _grid_members", sql)
        self.assertNotIn("INSERT INTO _personnel_attendance_history", sql)


if __name__ == "__main__":
    unittest.main()
