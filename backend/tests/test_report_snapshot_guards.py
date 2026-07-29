import os
import sqlite3
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
    def test_unable_to_verify_metric_does_not_include_unchecked_rows(self):
        builder = FullChainBuilder()
        workload_sql, _ = builder._build_workload_sql(
            "`2026-07-27_snapshot_fullChain`",
            "`2026-07-26_snapshot_fullChain`",
        )
        range_sql, _ = builder.build_stats_sql("(`snapshot_union`)")

        for sql in (workload_sql, range_sql):
            normalized_sql = " ".join(sql.split())
            self.assertIn(
                "SUM(CASE WHEN t.核查结果 LIKE '%%无法核实%%' "
                "THEN 1 ELSE 0 END)",
                normalized_sql,
            )
            self.assertNotIn(
                "t.核查结果 LIKE '%%无法核实%%' "
                "OR t.核查结果 IS NULL",
                normalized_sql,
            )

    def test_changed_rows_are_classified_by_current_status(self):
        inspector_sql, _ = FullChainBuilder()._build_workload_sql(
            "`2026-07-27_snapshot_fullChain`",
            "`2026-07-26_snapshot_fullChain`",
        )
        normalized_sql = " ".join(inspector_sql.split())

        self.assertIn(
            "IFNULL(t.现住址, '') = '' AND IFNULL(t.核查结果, '') = ''",
            normalized_sql,
        )
        self.assertIn(
            "IFNULL(t.现住址, '') <> '' AND IFNULL(t.核查结果, '') = ''",
            normalized_sql,
        )
        self.assertIn(
            "IFNULL(t.核查结果, '') <> ''",
            normalized_sql,
        )
        self.assertNotIn(
            "prev._row_key IS NULL AND IFNULL(t.现住址, '')",
            normalized_sql,
        )

    def test_address_correction_still_counts_as_checked(self):
        connection = sqlite3.connect(":memory:")
        try:
            for table in ("today_snapshot", "previous_snapshot"):
                connection.execute(
                    f"""
                    CREATE TABLE {table} (
                        _row_key TEXT PRIMARY KEY,
                        社区 TEXT,
                        核查人 TEXT,
                        现住址 TEXT,
                        核查结果 TEXT
                    )
                    """
                )
            previous_rows = [
                ("checked", "社区甲", "吕强", "旧地址", ""),
                ("done_address", "社区甲", "吕强", "旧地址", "已登记"),
                ("done_result", "社区甲", "吕强", "地址三", "已登记"),
                ("unchanged", "社区甲", "吕强", "地址四", "已登记"),
            ]
            today_rows = [
                ("checked", "社区甲", "吕强", "新地址", ""),
                ("done_address", "社区甲", "吕强", "新地址", "已登记"),
                ("done_result", "社区甲", "吕强", "地址三", "移交"),
                ("unchanged", "社区甲", "吕强", "地址四", "已登记"),
                ("new_unchecked", "社区甲", "吕强", "", ""),
            ]
            connection.executemany(
                "INSERT INTO previous_snapshot VALUES (?, ?, ?, ?, ?)",
                previous_rows,
            )
            connection.executemany(
                "INSERT INTO today_snapshot VALUES (?, ?, ?, ?, ?)",
                today_rows,
            )

            inspector_sql, _ = FullChainBuilder()._build_workload_sql(
                "today_snapshot",
                "previous_snapshot",
            )
            row = connection.execute(inspector_sql).fetchone()

            self.assertIsNotNone(row)
            self.assertEqual(row[2:6], (4, 1, 1, 2))
            self.assertEqual(sum(row[3:6]), row[2])
        finally:
            connection.close()

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

    async def test_range_with_snapshot_uses_task_ledger(self):
        snapshot_date = "2026-07-27"
        inspector_row = ("社区甲", "张三", 1, 1, 0, 0, 0, 1, 0)
        pool, cursor = make_database(
            fetchall_values=[
                [(snapshot_date,)],
                [(snapshot_date,)],
                [inspector_row],
            ]
        )

        with patch.object(
            report_range.db_manager,
            "get_pool",
            return_value=pool,
        ), patch.object(
            report_range,
            "complete_inspector_rows",
            new=AsyncMock(return_value=[inspector_row]),
        ):
            result = await report_range.get_report_range(
                "2026-07-27", "2026-07-28", "全链条"
            )

        self.assertTrue(result["exists"])
        self.assertEqual(result["range"]["days"], 1)
        executed_sql = [call.args[0] for call in cursor.execute.await_args_list]
        self.assertTrue(any("_daily_task_ledger" in sql for sql in executed_sql))
        self.assertTrue(
            any(
                "PARTITION BY ledger.parser_type, ledger.row_key" in sql
                for sql in executed_sql
            )
        )
        self.assertEqual(
            cursor.execute.await_args_list[-1].args[1],
            ("2026-07-27", "2026-07-28", "全链条"),
        )

    async def test_range_with_snapshot_but_without_ledger_requests_backfill(self):
        pool, _ = make_database(
            fetchall_values=[[("2026-07-27",)], []]
        )

        with patch.object(report_range.db_manager, "get_pool", return_value=pool):
            result = await report_range.get_report_range(
                "2026-07-27", "2026-07-27", "全链条"
            )

        self.assertFalse(result["exists"])
        self.assertIn("任务流水尚未生成", result["message"])

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
