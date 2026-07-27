from datetime import date
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.sync_engine import SyncEngine


class SyncSnapshotTimezoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_sync_rebuilds_summary_after_all_daily_reports(self):
        connection = MagicMock()
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=connection)
        pool.release = MagicMock()
        engine = SyncEngine(pool)
        engine._set_status = AsyncMock()
        engine._get_oauth_creds = AsyncMock(
            return_value={
                "client_id": "client",
                "access_token": "token",
                "open_id": "user",
            }
        )
        engine._get_spreadsheets = AsyncMock(
            return_value=[
                {"name": "全链条"},
                {"name": "出租房屋核查"},
            ]
        )
        engine._sync_one = AsyncMock(
            side_effect=[
                (15, "2026-07-27"),
                (8, "2026-07-27"),
            ]
        )
        engine._set_progress = AsyncMock()
        engine._complete = AsyncMock()
        engine._complete_with_errors = AsyncMock()
        engine._fail = AsyncMock()

        client = MagicMock()
        client.close = AsyncMock()
        summary_result = {
            "implemented": True,
            "date": "2026-07-27",
        }

        with patch(
            "services.sync_engine.TxDocsClient",
            return_value=client,
        ), patch(
            "services.report_builders.summary.build_summary",
            new=AsyncMock(return_value=summary_result),
        ) as build_summary:
            await engine.run_full_sync(123)

        build_summary.assert_awaited_once_with("2026-07-27")
        engine._complete.assert_awaited_once_with(connection, 123, 23)
        engine._complete_with_errors.assert_not_awaited()
        engine._fail.assert_not_awaited()
        client.close.assert_awaited_once()

    async def test_snapshot_uses_business_date(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor_context = MagicMock()
        cursor_context.__aenter__ = AsyncMock(return_value=cursor)
        cursor_context.__aexit__ = AsyncMock(return_value=None)
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        builder = MagicMock()
        builder.table_suffix = "fullChain"
        # BaseReportBuilder.build 成功时不返回 implemented 字段。
        builder.build = AsyncMock(
            return_value={
                "date": "2026-07-27",
                "type": "全链条",
                "inspector_rows": 1,
                "community_rows": 1,
            }
        )

        with patch(
            "services.sync_engine.get_business_date",
            new=AsyncMock(return_value=date(2026, 7, 27)),
        ), patch(
            "services.report_builders.BUILDERS",
            {"全链条": builder},
        ):
            result = await SyncEngine(None)._save_snapshot(
                connection,
                "t_fullchain",
                "全链条",
            )

        executed_sql = [call.args[0] for call in cursor.execute.await_args_list]
        self.assertTrue(
            any("2026-07-27_snapshot_fullChain" in sql for sql in executed_sql)
        )
        self.assertEqual(result, "2026-07-27")
        builder.build.assert_awaited_once_with("2026-07-27")


if __name__ == "__main__":
    unittest.main()
