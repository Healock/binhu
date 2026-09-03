import os
from datetime import date
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services import local_report_scheduler


def cursor_connection():
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=(1,))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=cursor)
    context.__aexit__ = AsyncMock(return_value=None)
    connection = MagicMock()
    connection.cursor.return_value = context
    connection.begin = AsyncMock()
    connection.commit = AsyncMock()
    connection.rollback = AsyncMock()
    return connection, cursor


class LocalReportSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_replaces_all_snapshots_without_runtime_metadata_rename(self):
        connection, cursor = cursor_connection()
        fullchain = MagicMock(source_table="t_fullchain", table_suffix="fullChain")
        rental = MagicMock(source_table="t_rental_check", table_suffix="rentalCheck")

        with patch.object(
            local_report_scheduler,
            "BUILDERS",
            {"全链条": fullchain, "出租房屋核查": rental},
        ), patch.object(
            local_report_scheduler,
            "get_business_date",
            new=AsyncMock(return_value=date(2026, 8, 30)),
        ), patch.object(
            local_report_scheduler,
            "table_exists",
            new=AsyncMock(side_effect=[True, False]),
        ):
            result = await local_report_scheduler.replace_local_report_snapshots(
                connection
            )

        self.assertEqual(result, "2026-08-30")
        sql = [call.args[0] for call in cursor.execute.await_args_list]
        create_positions = [
            index for index, statement in enumerate(sql)
            if statement.startswith("CREATE TABLE")
        ]
        insert_positions = [
            index for index, statement in enumerate(sql)
            if statement.startswith("INSERT INTO") and "snapshot_" in statement
        ]
        # The second table is created on first use; existing tables are reused.
        self.assertEqual(len(create_positions), 1)
        self.assertEqual(len(insert_positions), 2)
        connection.begin.assert_awaited_once()
        connection.commit.assert_awaited_once()
        connection.rollback.assert_not_awaited()
        self.assertFalse(any(item.startswith("RENAME TABLE") for item in sql))
        self.assertEqual(
            sum("_daily_report_meta" in item for item in sql),
            1,
        )

    async def test_failed_snapshot_read_keeps_current_tables(self):
        connection, cursor = cursor_connection()
        cursor.execute.side_effect = [
            None,
            None,
            None,
            RuntimeError("read failed"),
            None,
            None,
        ]
        fullchain = MagicMock(source_table="t_fullchain", table_suffix="fullChain")
        rental = MagicMock(source_table="t_rental_check", table_suffix="rentalCheck")

        with patch.object(
            local_report_scheduler,
            "BUILDERS",
            {"全链条": fullchain, "出租房屋核查": rental},
        ), patch.object(
            local_report_scheduler,
            "get_business_date",
            new=AsyncMock(return_value=date(2026, 8, 30)),
        ):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                await local_report_scheduler.replace_local_report_snapshots(connection)

        sql = [call.args[0] for call in cursor.execute.await_args_list]
        self.assertFalse(any(item.startswith("RENAME TABLE") for item in sql))
        connection.rollback.assert_awaited_once()

    async def test_metadata_failure_rolls_back_stable_snapshot_tables(self):
        connection, cursor = cursor_connection()
        fullchain = MagicMock(source_table="t_fullchain", table_suffix="fullChain")

        async def execute(sql, params=None):
            if "_daily_report_meta" in str(sql):
                raise RuntimeError("metadata failed")

        cursor.execute.side_effect = execute
        with patch.object(
            local_report_scheduler,
            "BUILDERS",
            {"全链条": fullchain},
        ), patch.object(
            local_report_scheduler,
            "get_business_date",
            new=AsyncMock(return_value=date(2026, 8, 30)),
        ), patch.object(
            local_report_scheduler,
            "table_exists",
            new=AsyncMock(return_value=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "metadata failed"):
                await local_report_scheduler.replace_local_report_snapshots(connection)

        self.assertFalse(any(
            call.args[0].startswith("RENAME TABLE")
            for call in cursor.execute.await_args_list
        ))
        connection.rollback.assert_awaited_once()


class LocalReportRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshes_snapshots_then_all_subreports_then_summary(self):
        events = []
        connection = MagicMock()
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=connection)
        pool.release = MagicMock()
        fullchain = MagicMock()
        rental = MagicMock()

        async def build_fullchain(report_date, generation_method=None):
            events.append(("report", "全链条", report_date, generation_method))
            return {"implemented": True}

        async def build_rental(report_date, generation_method=None):
            events.append(("report", "出租房屋核查", report_date, generation_method))
            return {"implemented": True}

        fullchain.build = AsyncMock(side_effect=build_fullchain)
        rental.build = AsyncMock(side_effect=build_rental)

        async def replace_snapshots(_conn, parser_types):
            events.append(("snapshots", tuple(parser_types)))
            return "2026-08-30"

        async def build_total(report_date, summary_types=None, generation_method=None):
            events.append(
                ("summary", report_date, tuple(summary_types), generation_method)
            )
            return {"implemented": True}

        with patch.object(
            local_report_scheduler.db_manager,
            "get_pool",
            return_value=pool,
        ), patch.object(
            local_report_scheduler,
            "_acquire_refresh_lock",
            new=AsyncMock(return_value=True),
        ), patch.object(
            local_report_scheduler,
            "_release_refresh_lock",
            new=AsyncMock(),
        ) as release_lock, patch.object(
            local_report_scheduler,
            "replace_local_report_snapshots",
            new=AsyncMock(side_effect=replace_snapshots),
        ), patch.object(
            local_report_scheduler,
            "BUILDERS",
            {"全链条": fullchain, "出租房屋核查": rental},
        ), patch.object(
            local_report_scheduler,
            "_load_summary_types",
            new=AsyncMock(return_value=["全链条"]),
        ), patch.object(
            local_report_scheduler,
            "build_summary",
            new=AsyncMock(side_effect=build_total),
        ):
            result = await local_report_scheduler.refresh_local_daily_reports_once()

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            events,
            [
                ("snapshots", ("全链条", "出租房屋核查")),
                ("report", "全链条", "2026-08-30", "local_scheduler"),
                ("report", "出租房屋核查", "2026-08-30", "local_scheduler"),
                ("summary", "2026-08-30", ("全链条",), "local_scheduler"),
            ],
        )
        release_lock.assert_awaited_once_with(connection)
        pool.release.assert_called_once_with(connection)

    async def test_lock_contention_skips_without_touching_reports(self):
        connection = MagicMock()
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=connection)
        pool.release = MagicMock()
        replace_snapshots = AsyncMock()

        with patch.object(
            local_report_scheduler.db_manager,
            "get_pool",
            return_value=pool,
        ), patch.object(
            local_report_scheduler,
            "_acquire_refresh_lock",
            new=AsyncMock(return_value=False),
        ), patch.object(
            local_report_scheduler,
            "replace_local_report_snapshots",
            new=replace_snapshots,
        ):
            result = await local_report_scheduler.refresh_local_daily_reports_once()

        self.assertEqual(result["status"], "busy")
        replace_snapshots.assert_not_awaited()
        pool.release.assert_called_once_with(connection)

    async def test_failed_subreport_does_not_replace_total_summary(self):
        connection = MagicMock()
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=connection)
        pool.release = MagicMock()
        builder = MagicMock()
        builder.build = AsyncMock(
            return_value={"implemented": False, "message": "分汇总失败"}
        )
        total = AsyncMock()

        with patch.object(
            local_report_scheduler.db_manager,
            "get_pool",
            return_value=pool,
        ), patch.object(
            local_report_scheduler,
            "_acquire_refresh_lock",
            new=AsyncMock(return_value=True),
        ), patch.object(
            local_report_scheduler,
            "_release_refresh_lock",
            new=AsyncMock(),
        ), patch.object(
            local_report_scheduler,
            "replace_local_report_snapshots",
            new=AsyncMock(return_value="2026-08-30"),
        ), patch.object(
            local_report_scheduler,
            "BUILDERS",
            {"全链条": builder},
        ), patch.object(
            local_report_scheduler,
            "build_summary",
            new=total,
        ):
            with self.assertRaisesRegex(RuntimeError, "分汇总失败"):
                await local_report_scheduler.refresh_local_daily_reports_once()

        total.assert_not_awaited()


class LocalReportWiringTests(unittest.TestCase):
    def test_application_lifespan_starts_and_stops_local_report_scheduler(self):
        from pathlib import Path

        source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("run_local_report_scheduler", source)
        self.assertIn("local_report_task = asyncio.create_task", source)
        self.assertIn("local_report_task.cancel()", source)
        self.assertIn("await local_report_task", source)


if __name__ == "__main__":
    unittest.main()
