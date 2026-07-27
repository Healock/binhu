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
    async def test_daily_summary_rate_uses_completed_minus_unable(self):
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
            "GREATEST(已完成 - 无法见底数, 0) / 数据总数",
            normalized,
        )
        self.assertNotIn(
            "(数据总数 - 无法见底数) / 数据总数",
            normalized,
        )

    async def test_range_summary_rate_uses_completed_minus_unable(self):
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
            "_find_snapshots",
            new=AsyncMock(return_value=["2026-07-27_snapshot_fullChain"]),
        ), patch.object(
            report_range,
            "get_business_date_range_utc_bounds",
            new=AsyncMock(
                return_value=(
                    "2026-07-27 00:00:00",
                    "2026-07-28 00:00:00",
                )
            ),
        ):
            result = await report_range.get_summary_range(
                "2026-07-27",
                "2026-07-27",
            )

        self.assertTrue(result["exists"])
        summary_sql = cursor.execute.await_args_list[-1].args[0]
        normalized = " ".join(summary_sql.split())
        self.assertIn(
            "GREATEST(SUM(t.已完成) - SUM(t.无法见底数), 0) "
            "/ SUM(t.数据总数)",
            normalized,
        )
        self.assertNotIn(
            "SUM(t.数据总数) - SUM(t.无法见底数)",
            normalized,
        )


if __name__ == "__main__":
    unittest.main()
