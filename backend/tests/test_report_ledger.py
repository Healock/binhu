import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services import report_ledger
from services.report_builders.fullchain import FullChainBuilder
from services.report_builders.suspect_unrevoked import SuspectUnrevokedBuilder


def make_cursor(fetchone_values):
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(side_effect=fetchone_values)
    return cursor


class DailyTaskLedgerTests(unittest.IsolatedAsyncioTestCase):
    def test_standard_status_covers_all_three_columns(self):
        builder = FullChainBuilder()
        sql = builder.ledger_state_sql("task")
        self.assertIn("'completed'", sql)
        self.assertIn("'checked'", sql)
        self.assertIn("'unchecked'", sql)
        self.assertIn("task.`现住址`", sql)
        # 这两个表达式会和数据库参数一起执行；百分号必须转义，
        # 否则 aiomysql 会把它误当作 Python 格式化符。
        self.assertIn("%%无法核实%%", builder.ledger_unable_sql("task"))
        self.assertIn("%%已登记%%", builder.ledger_reached_bottom_sql("task"))

    def test_model_three_has_only_unchecked_and_completed(self):
        builder = SuspectUnrevokedBuilder()
        self.assertIn("'completed'", builder.ledger_state_sql("task"))
        self.assertNotIn("'checked'", builder.ledger_state_sql("task"))
        self.assertEqual(builder.ledger_unable_sql("task"), "0")
        self.assertIn("近期反吴", builder.ledger_reached_bottom_sql("task"))

    async def test_carried_task_changed_today_has_one_carryover_row(self):
        cursor = make_cursor(
            [
                (datetime(2026, 7, 29, 1, 0),),
                ("2026-07-28_snapshot_fullChain",),
                (1, 1),
            ]
        )
        builder = FullChainBuilder()

        with patch.object(
            report_ledger,
            "get_business_date",
            new=AsyncMock(return_value=datetime(2026, 7, 29).date()),
        ):
            result = await report_ledger.refresh_daily_ledger(
                cursor,
                builder,
                "2026-07-29",
            )

        self.assertEqual(result["included_rows"], 1)
        sql = "\n".join(
            call.args[0] for call in cursor.execute.await_args_list
        )
        self.assertIn("candidate.previous_unfinished=1", sql)
        self.assertIn("THEN 'carryover'", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)

    async def test_removed_unfinished_task_is_kept_as_excluded_tombstone(self):
        cursor = make_cursor(
            [
                (datetime(2026, 7, 29, 1, 0),),
                ("2026-07-28_snapshot_fullChain",),
                (1, 0),
            ]
        )
        builder = FullChainBuilder()

        with patch.object(
            report_ledger,
            "get_business_date",
            new=AsyncMock(return_value=datetime(2026, 7, 29).date()),
        ):
            await report_ledger.refresh_daily_ledger(
                cursor,
                builder,
                "2026-07-29",
            )

        sql = "\n".join(
            call.args[0] for call in cursor.execute.await_args_list
        )
        self.assertIn("source='removed'", sql)
        self.assertIn("included=0", sql)
        self.assertIn("LEFT JOIN `2026-07-29_snapshot_fullChain` t", sql)


if __name__ == "__main__":
    unittest.main()
