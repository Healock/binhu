from datetime import date
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.sync_engine import SyncEngine


def make_engine():
    connection = MagicMock()
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=connection)
    pool.release = MagicMock()
    engine = SyncEngine(pool)
    engine._set_status = AsyncMock()
    engine._set_current = AsyncMock()
    engine._set_progress = AsyncMock()
    engine._advance_step = AsyncMock()
    engine._set_total_steps = AsyncMock()
    engine._set_phase = AsyncMock()
    engine._complete = AsyncMock()
    engine._complete_with_errors = AsyncMock()
    engine._fail = AsyncMock()
    engine._get_oauth_creds = AsyncMock(
        return_value={
            "client_id": "client",
            "access_token": "token",
            "open_id": "user",
        }
    )
    return engine, connection


class SyncSnapshotTimezoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_sync_builds_all_subreports_before_summary(self):
        events = []
        engine, connection = make_engine()
        engine._get_spreadsheets = AsyncMock(
            return_value=[
                {
                    "name": "全链条",
                    "parser_type": "全链条",
                },
                {
                    "name": "出租房屋核查",
                    "parser_type": "出租房屋核查",
                },
            ]
        )

        async def sync_one(_conn, _client, spreadsheet):
            events.append(("sync", spreadsheet["parser_type"]))
            return (
                15 if spreadsheet["parser_type"] == "全链条" else 8,
                "2026-07-27",
            )

        engine._sync_one = AsyncMock(side_effect=sync_one)
        fullchain = MagicMock()
        rental = MagicMock()

        async def build_fullchain(report_date):
            events.append(("report", "全链条"))
            return {"date": report_date, "implemented": True}

        async def build_rental(report_date):
            events.append(("report", "出租房屋核查"))
            return {"date": report_date, "implemented": True}

        fullchain.build = AsyncMock(side_effect=build_fullchain)
        rental.build = AsyncMock(side_effect=build_rental)
        client = MagicMock()
        client.close = AsyncMock()

        async def build_total(report_date, summary_types=None):
            events.append(("summary", "总汇总表"))
            self.assertEqual(
                summary_types,
                ["全链条", "出租房屋核查"],
            )
            return {
                "implemented": True,
                "date": report_date,
            }

        with patch(
            "services.sync_engine.TxDocsClient",
            return_value=client,
        ), patch(
            "services.report_builders.BUILDERS",
            {"全链条": fullchain, "出租房屋核查": rental},
        ), patch(
            "services.report_builders.summary._load_summary_types",
            new=AsyncMock(
                return_value=["全链条", "出租房屋核查"],
            ),
        ), patch(
            "services.report_builders.summary.build_summary",
            new=AsyncMock(side_effect=build_total),
        ):
            await engine.run_full_sync(123)

        self.assertEqual(
            events,
            [
                ("sync", "全链条"),
                ("sync", "出租房屋核查"),
                ("report", "全链条"),
                ("report", "出租房屋核查"),
                ("summary", "总汇总表"),
            ],
        )
        engine._complete.assert_awaited_once_with(connection, 123, 23)
        engine._complete_with_errors.assert_not_awaited()
        engine._fail.assert_not_awaited()
        client.close.assert_awaited_once()

    async def test_partial_sync_builds_successful_report_but_skips_summary(self):
        engine, connection = make_engine()
        engine._get_spreadsheets = AsyncMock(
            return_value=[
                {"name": "全链条", "parser_type": "全链条"},
                {
                    "name": "出租房屋核查",
                    "parser_type": "出租房屋核查",
                },
            ]
        )
        engine._sync_one = AsyncMock(
            side_effect=[
                (15, "2026-07-27"),
                RuntimeError("腾讯文档读取失败"),
            ]
        )
        fullchain = MagicMock()
        fullchain.build = AsyncMock(
            return_value={"implemented": True},
        )
        client = MagicMock()
        client.close = AsyncMock()
        build_summary = AsyncMock()

        with patch(
            "services.sync_engine.TxDocsClient",
            return_value=client,
        ), patch(
            "services.report_builders.BUILDERS",
            {"全链条": fullchain},
        ), patch(
            "services.report_builders.summary.build_summary",
            new=build_summary,
        ):
            await engine.run_full_sync(124)

        fullchain.build.assert_awaited_once_with("2026-07-27")
        build_summary.assert_not_awaited()
        engine._complete.assert_not_awaited()
        engine._complete_with_errors.assert_awaited_once()
        args = engine._complete_with_errors.await_args.args
        self.assertIs(args[0], connection)
        self.assertEqual(args[1:3], (124, 15))
        self.assertIn("出租房屋核查", args[3])

    async def test_existing_model_three_snapshot_is_added_to_report_batch(self):
        events = []
        engine, connection = make_engine()
        engine._get_spreadsheets = AsyncMock(
            return_value=[{"name": "全链条", "parser_type": "全链条"}]
        )
        engine._sync_one = AsyncMock(return_value=(3, "2026-07-27"))
        engine._has_daily_report_snapshot = AsyncMock(return_value=True)

        fullchain = MagicMock()
        model_three = MagicMock()
        fullchain.build = AsyncMock(
            side_effect=lambda report_date: events.append(
                ("report", "全链条", report_date)
            ) or {"implemented": True}
        )
        model_three.build = AsyncMock(
            side_effect=lambda report_date: events.append(
                ("report", "疑似未注销模型三", report_date)
            ) or {"implemented": True}
        )
        model_three.table_suffix = "suspectUnrevoked"
        client = MagicMock()
        client.close = AsyncMock()

        async def build_total(report_date, summary_types=None):
            events.append(("summary", summary_types))
            return {"implemented": True}

        with patch(
            "services.sync_engine.TxDocsClient", return_value=client
        ), patch(
            "services.report_builders.BUILDERS",
            {
                "全链条": fullchain,
                "疑似未注销模型三": model_three,
            },
        ), patch(
            "services.report_builders.summary._load_summary_types",
            new=AsyncMock(return_value=["全链条", "疑似未注销模型三"]),
        ), patch(
            "services.report_builders.summary.build_summary",
            new=AsyncMock(side_effect=build_total),
        ):
            await engine.run_full_sync(125)

        fullchain.build.assert_awaited_once_with("2026-07-27")
        model_three.build.assert_awaited_once_with("2026-07-27")
        self.assertIn(
            ("summary", ["全链条", "疑似未注销模型三"]),
            events,
        )
        engine._complete.assert_awaited_once_with(connection, 125, 3)

    async def test_snapshot_uses_business_date_without_building_report(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        cursor_context = MagicMock()
        cursor_context.__aenter__ = AsyncMock(return_value=cursor)
        cursor_context.__aexit__ = AsyncMock(return_value=None)
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        builder = MagicMock()
        builder.table_suffix = "fullChain"
        builder.build = AsyncMock()

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
        self.assertFalse(any("DROP TABLE" in sql for sql in executed_sql))
        self.assertEqual(result, "2026-07-27")
        builder.build.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
