from datetime import date
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services import report_attendance, report_range
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
    async def test_person_days_loads_from_online_data_pool(self):
        pool, _ = make_database()
        expected = ({"长板": 2}, {"complete": True})
        with patch.object(
            report_attendance.db_manager,
            "get_pool",
            return_value=pool,
        ) as get_pool, patch.object(
            report_attendance,
            "get_community_person_days",
            new=AsyncMock(return_value=expected),
        ):
            result = await report_attendance.load_community_person_days(
                {"2026-07-27"},
                {"长板": "长板"},
            )

        self.assertEqual(result, expected)
        get_pool.assert_called_once_with("online_data")
        pool.release.assert_called_once()

    async def test_person_days_use_leave_and_weekend_duty(self):
        context = {
            "members": {
                "张三": {
                    "id": 1,
                    "name": "张三",
                    "community": "长板",
                    "communities": ["长板"],
                    "position": "组员",
                },
                "李四": {
                    "id": 2,
                    "name": "李四",
                    "community": "长板",
                    "communities": ["长板"],
                    "position": "组员",
                },
            },
            "periods": {
                2: [{
                    "start_date": date(2026, 7, 28),
                    "end_date": date(2026, 7, 28),
                    "is_active": True,
                }],
            },
            "duties": {(1, date(2026, 7, 27)): date(2026, 8, 1)},
            "weekend_duty_positions": {"组长", "组员"},
            "missing_week_starts": set(),
            "history_started_on": date(2026, 7, 1),
            "legacy_history_incomplete": False,
        }
        with patch.object(
            report_attendance,
            "get_attendance_context",
            new=AsyncMock(return_value=context),
        ):
            person_days, attendance = (
                await report_attendance.get_community_person_days(
                    MagicMock(),
                    {"2026-07-27", "2026-07-28", "2026-08-01"},
                    {"长板": "长板"},
                )
            )

        self.assertEqual(person_days, {"长板": 4})
        self.assertTrue(attendance["complete"])

    async def test_person_days_hide_average_for_covered_unassigned_weekend(self):
        context = {
            "members": {},
            "periods": {},
            "duties": {},
            "weekend_duty_positions": {"组长", "组员"},
            "missing_week_starts": {date(2026, 7, 27)},
            "history_started_on": date(2026, 7, 30),
            "legacy_history_incomplete": True,
        }
        with patch.object(
            report_attendance,
            "get_attendance_context",
            new=AsyncMock(return_value=context),
        ):
            _, attendance = await report_attendance.get_community_person_days(
                MagicMock(),
                {"2026-08-01"},
                {},
            )

        self.assertFalse(attendance["complete"])
        self.assertEqual(attendance["missing_week_starts"], ["2026-07-27"])

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
        insert_sql = next(
            call.args[0]
            for call in cursor.execute.await_args_list
            if "INSERT INTO `2026-07-27_daily_summary`" in call.args[0]
        )
        normalized_insert = " ".join(insert_sql.split())
        self.assertIn(
            "LEFT JOIN OnlineData._community_aliases",
            normalized_insert,
        )
        self.assertIn(
            "GROUP BY COALESCE(formal_community.name, t.社区)",
            normalized_insert,
        )
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
            new=AsyncMock(
                return_value=(["2026-07-27", "2026-07-28"], [])
            ),
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
        ), patch.object(
            report_range,
            "_load_community_person_days",
            new=AsyncMock(return_value=(
                {"长板": 5},
                {
                    "complete": True,
                    "missing_week_starts": [],
                    "history_started_on": "2026-07-01",
                    "legacy_history_incomplete": False,
                },
            )),
        ):
            result = await report_range.get_summary_range(
                "2026-07-27",
                "2026-07-28",
            )

        self.assertTrue(result["exists"])
        row = result["data"][0]
        self.assertEqual(row["核查完成率"], 0.83)
        self.assertEqual(row["核查见底率"], 0.8)
        self.assertEqual(row["在岗人日"], 5)
        self.assertEqual(row["每日人均核查数"], 2.0)
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
