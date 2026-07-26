import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

try:
    import aiomysql  # noqa: F401
except ModuleNotFoundError:
    aiomysql_stub = types.ModuleType("aiomysql")
    aiomysql_stub.Pool = object
    sys.modules["aiomysql"] = aiomysql_stub

from services import report_range
from services.report_builders import base as report_base
from services.report_builders import summary as report_summary
from services.report_builders.fullchain import FullChainBuilder
from services.stats_calculator import DailyReportBuilder
from services import stats_calculator


def make_database(fetchone_values=None, fetchall_values=None):
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(
        side_effect=fetchone_values if fetchone_values is not None else None
    )
    cursor.fetchall = AsyncMock(
        side_effect=fetchall_values if fetchall_values is not None else None
    )

    cursor_context = MagicMock()
    cursor_context.__aenter__ = AsyncMock(return_value=cursor)
    cursor_context.__aexit__ = AsyncMock(return_value=None)

    connection = MagicMock()
    connection.cursor.return_value = cursor_context

    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=connection)
    pool.release = MagicMock()
    return pool, cursor


class ReportSnapshotGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_range_without_snapshots_does_not_query_live_table(self):
        pool, cursor = make_database(fetchall_values=[[]])

        with patch.object(report_range.db_manager, "get_pool", return_value=pool):
            result = await report_range.get_report_range(
                "2026-07-08", "2026-07-10", "全链条"
            )

        self.assertFalse(result["exists"])
        self.assertIn("没有同步快照", result["message"])
        executed_sql = [call.args[0] for call in cursor.execute.await_args_list]
        self.assertFalse(any("OnlineData.t_fullchain" in sql for sql in executed_sql))
        self.assertEqual(len(executed_sql), 1)

    async def test_range_with_snapshot_still_uses_snapshot_query(self):
        snapshot = "2026-07-27_snapshot_fullChain"
        inspector_row = ("社区甲", "张三", 1, 1, 0, 0, 0, 1, 0)
        community_row = ("社区甲", 1, 1, 0, 0, 0, 1, 0)
        pool, cursor = make_database(
            fetchall_values=[[(snapshot,)], [inspector_row], [community_row]]
        )

        with patch.object(report_range.db_manager, "get_pool", return_value=pool):
            result = await report_range.get_report_range(
                "2026-07-27", "2026-07-28", "全链条"
            )

        self.assertTrue(result["exists"])
        self.assertEqual(result["range"]["days"], 1)
        executed_sql = [call.args[0] for call in cursor.execute.await_args_list]
        self.assertTrue(any(snapshot in sql for sql in executed_sql))
        self.assertTrue(
            any(
                "_first_seen_at >= %s AND _first_seen_at < %s" in sql
                for sql in executed_sql
            )
        )

    async def test_build_without_today_snapshot_creates_no_report_tables(self):
        pool, cursor = make_database(fetchone_values=[None])
        builder = FullChainBuilder()

        with patch.object(report_base.db_manager, "get_pool", return_value=pool):
            result = await builder.build("2026-07-08")

        self.assertFalse(result["implemented"])
        self.assertIn("没有同步快照", result["message"])
        executed_sql = [call.args[0] for call in cursor.execute.await_args_list]
        self.assertEqual(len(executed_sql), 1)
        self.assertFalse(any("CREATE TABLE" in sql for sql in executed_sql))
        self.assertFalse(any("TRUNCATE TABLE" in sql for sql in executed_sql))

    async def test_dispatcher_preserves_missing_snapshot_result(self):
        builder = MagicMock()
        builder.build = AsyncMock(
            return_value={"implemented": False, "message": "没有同步快照"}
        )

        with patch.object(stats_calculator, "get_builder", return_value=builder):
            result = await DailyReportBuilder().build("2026-07-08", "全链条")

        self.assertFalse(result["implemented"])

    async def test_single_day_report_requires_same_day_snapshot(self):
        pool, cursor = make_database(fetchone_values=[None])

        with patch.object(stats_calculator.db_manager, "get_pool", return_value=pool):
            result = await DailyReportBuilder().get_report("2026-07-08", "全链条")

        self.assertFalse(result["exists"])
        self.assertIn("没有同步快照", result["message"])
        self.assertEqual(cursor.execute.await_count, 1)

    async def test_summary_report_requires_at_least_one_same_day_snapshot(self):
        pool, cursor = make_database(fetchone_values=[None])

        with patch.object(report_summary.db_manager, "get_pool", return_value=pool):
            result = await report_summary.get_summary("2026-07-08")

        self.assertFalse(result["exists"])
        self.assertIn("没有同步快照", result["message"])
        self.assertEqual(cursor.execute.await_count, 1)


if __name__ == "__main__":
    unittest.main()
