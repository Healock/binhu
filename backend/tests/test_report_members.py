import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

try:
    import aiomysql  # noqa: F401
except ModuleNotFoundError:
    aiomysql_stub = types.ModuleType("aiomysql")
    aiomysql_stub.Pool = object
    sys.modules["aiomysql"] = aiomysql_stub

from services.report_members import (
    complete_inspector_rows,
    get_active_members,
    get_missing_zero_rows,
    insert_zero_member_rows,
)


class ReportMemberCompletionTests(unittest.IsolatedAsyncioTestCase):
    def test_missing_active_member_gets_zero_row_without_duplicates(self):
        existing = [
            ("业务社区", "张三", 2, 0, 0, 2, 1, 0, 1),
            ("社区外", "名册外人员", 1, 0, 1, 0, 0, 0, 1),
        ]
        active_members = [
            ("名册社区", " 张三 "),
            ("社区乙", "李四"),
            ("社区乙", "李四"),
        ]

        missing = get_missing_zero_rows(existing, active_members)

        self.assertEqual(
            missing,
            [("社区乙", "李四", 0, 0, 0, 0, 0, 0, 0)],
        )

    async def test_active_member_query_uses_requested_date_and_status_rules(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(
            return_value=[("", "王五"), ("社区甲", "赵六")]
        )

        members = await get_active_members(cursor, "2026-07-28")

        sql, params = cursor.execute.await_args.args
        self.assertIn("g.status = '在岗'", sql)
        self.assertIn(
            "%s BETWEEN g.leave_start_date AND g.leave_end_date",
            sql,
        )
        self.assertEqual(params, ("2026-07-28",))
        self.assertEqual(
            members,
            [("未分配社区", "王五"), ("社区甲", "赵六")],
        )

    async def test_old_report_is_completed_without_losing_real_rows(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[("社区乙", "李四")])
        existing = [
            ("社区甲", "张三", 1, 0, 0, 1, 1, 0, 1),
            ("社区外", "名册外人员", 1, 1, 0, 0, 0, 1, 0),
            ("社区丙", "后来请假的人员", 0, 0, 0, 0, 0, 0, 0),
        ]

        completed = await complete_inspector_rows(
            cursor,
            existing,
            "2026-07-28",
        )

        self.assertEqual(len(completed), 3)
        self.assertIn(existing[0], completed)
        self.assertIn(existing[1], completed)
        self.assertIn(
            ("社区乙", "李四", 0, 0, 0, 0, 0, 0, 0),
            completed,
        )
        self.assertNotIn(existing[2], completed)

    async def test_new_report_persists_missing_zero_rows(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(
            side_effect=[
                [("社区甲", "张三")],
                [("社区甲", "张三"), ("社区乙", "李四")],
            ]
        )
        cursor.executemany = AsyncMock()

        inserted = await insert_zero_member_rows(
            cursor,
            "`2026-07-28_daily_fullChain_inspector`",
            "2026-07-28",
        )

        self.assertEqual(inserted, 1)
        rows = cursor.executemany.await_args.args[1]
        self.assertEqual(
            rows,
            [("社区乙", "李四", 0, 0, 0, 0, 0, 0, 0)],
        )


if __name__ == "__main__":
    unittest.main()
