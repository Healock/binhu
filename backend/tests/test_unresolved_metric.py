import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services import report_range
from services.report_builders import summary as report_summary


def make_database(*, fetchall=None, fetchone=None):
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=fetchall or [])
    cursor.fetchone = AsyncMock(return_value=fetchone)

    cursor_context = MagicMock()
    cursor_context.__aenter__ = AsyncMock(return_value=cursor)
    cursor_context.__aexit__ = AsyncMock(return_value=None)

    connection = MagicMock()
    connection.cursor.return_value = cursor_context

    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=connection)
    pool.release = MagicMock()
    return pool, cursor


class UnresolvedMetricTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_summary_rate_uses_completed_as_denominator(self):
        report_date = "2026-07-27"
        community_table = f"{report_date}_daily_fullChain_community"
        snapshot_table = f"{report_date}_snapshot_fullChain"
        pool, cursor = make_database(
            fetchall=[(community_table,), (snapshot_table,)],
            fetchone=(12,),
        )

        with patch.object(
            report_summary.db_manager,
            "get_pool",
            return_value=pool,
        ):
            result = await report_summary.build_summary(
                report_date,
                summary_types=["全链条"],
            )

        self.assertTrue(result["implemented"])
        update_sql = next(
            call.args[0]
            for call in cursor.execute.await_args_list
            if "UPDATE `2026-07-27_daily_summary` SET" in call.args[0]
        )
        normalized = " ".join(update_sql.split())
        self.assertIn(
            "GREATEST(已完成 - 无法见底数, 0) / 已完成",
            normalized,
        )
        self.assertNotIn(
            "GREATEST(已完成 - 无法见底数, 0) / 数据总数",
            normalized,
        )

    async def test_range_summary_preserves_completed_denominator_rate(self):
        pool, cursor = make_database(fetchall=[])

        with patch.object(
            report_range.db_manager,
            "get_pool",
            return_value=pool,
        ), patch.object(
            report_range,
            "_get_summary_types",
            new=AsyncMock(return_value=["全链条"]),
        ), patch.object(
            report_range,
            "_validate_ledger_coverage",
            new=AsyncMock(return_value=(["2026-07-27"], [])),
        ), patch.object(
            report_range,
            "_aggregate_range_ledger",
            new=AsyncMock(
                return_value=[("长板", "张三", 12, 0, 0, 10, 0.83, 2, 0.8)]
            ),
        ), patch.object(
            report_range,
            "complete_inspector_rows",
            new=AsyncMock(
                return_value=[
                    ("长板", "张三", 12, 0, 0, 10, 0, 2, 0),
                ]
            ),
        ), patch.object(
            report_range,
            "get_active_members",
            new=AsyncMock(return_value=[("长板", "张三")]),
        ):
            result = await report_range.get_summary_range(
                "2026-07-27",
                "2026-07-27",
            )

        self.assertTrue(result["exists"])
        row = result["data"][0]
        self.assertEqual(row["核查完成率"], 0.83)
        self.assertEqual(row["核查见底率"], 0.8)
        self.assertEqual(result["community"]["data"], result["data"])
        self.assertEqual(result["inspector"]["data"][0]["姓名"], "张三")
        self.assertEqual(
            result["inspector"]["data"][0]["核查完成率"],
            0.83,
        )

    async def test_range_ledger_rate_divides_by_completed_count(self):
        _, cursor = make_database(fetchall=[])

        await report_range._aggregate_range_ledger(
            cursor,
            "2026-07-27",
            "2026-07-29",
            "全链条",
        )

        sql = " ".join(cursor.execute.await_args.args[0].split())
        self.assertIn(
            "SUM(latest.reached_bottom) "
            "/ SUM(latest.task_state = 'completed')",
            sql,
        )
        self.assertNotIn(
            "SUM(latest.reached_bottom) / COUNT(*)",
            sql,
        )


if __name__ == "__main__":
    unittest.main()
