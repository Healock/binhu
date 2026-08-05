import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from routers.auth import UserPreferencesRequest, update_preferences
from routers.stats import get_report as get_report_endpoint
from services.report_view import project_report_payload


def sample_report():
    columns = [
        "社区",
        "数据总数",
        "未核查",
        "已核查",
        "已完成",
        "核查完成率",
    ]
    return {
        "exists": True,
        "columns": columns,
        "data": [
            {
                "社区": "长板",
                "数据总数": 30,
                "未核查": 10,
                "已核查": 5,
                "已完成": 15,
                "核查完成率": 0.5,
            }
        ],
    }


def sample_detailed_table():
    columns = [
        "社区",
        "数据总数",
        "未核查",
        "已核查",
        "已完成",
        "核查完成率",
        "无法见底数",
        "核查见底率",
    ]
    return {
        "columns": columns,
        "data": [
            {
                "社区": "长板",
                "数据总数": 30,
                "未核查": 10,
                "已核查": 5,
                "已完成": 15,
                "核查完成率": 0.5,
                "无法见底数": 3,
                "核查见底率": 0.8,
            },
            {
                "社区": "水秀",
                "数据总数": 20,
                "未核查": 4,
                "已核查": 6,
                "已完成": 10,
                "核查完成率": 0.5,
                "无法见底数": 2,
                "核查见底率": 0.8,
            },
        ],
    }


class ReportColumnModeTests(unittest.IsolatedAsyncioTestCase):
    def test_two_column_mode_merges_unfinished_work(self):
        source = sample_report()

        result = project_report_payload(source, "two")

        self.assertEqual(
            result["columns"],
            ["社区", "数据总数", "未核查", "已核查", "核查完成率"],
        )
        self.assertEqual(result["data"][0]["未核查"], 15)
        self.assertEqual(result["data"][0]["已核查"], 15)
        self.assertNotIn("已完成", result["data"][0])
        self.assertEqual(result["data"][0]["核查完成率"], 0.5)
        self.assertEqual(result["column_mode"], "two")

    def test_projection_does_not_change_canonical_payload(self):
        source = sample_report()

        project_report_payload(source, "two")

        self.assertEqual(source["data"][0]["未核查"], 10)
        self.assertEqual(source["data"][0]["已核查"], 5)
        self.assertEqual(source["data"][0]["已完成"], 15)

    def test_three_column_mode_keeps_all_status_columns(self):
        result = project_report_payload(sample_report(), "three")

        self.assertIn("已完成", result["columns"])
        self.assertEqual(result["data"][0]["已核查"], 5)
        self.assertEqual(result["column_mode"], "three")
        self.assertEqual(result["summary"]["社区"], "总计")
        self.assertEqual(result["summary"]["数据总数"], 30)

    def test_nested_inspector_and_community_tables_are_projected(self):
        table = sample_report()
        payload = {
            "exists": True,
            "inspector": {
                "columns": ["姓名", *table["columns"]],
                "data": [{"姓名": "张三", **table["data"][0]}],
            },
            "community": table,
        }

        result = project_report_payload(payload, "two")

        self.assertEqual(result["inspector"]["data"][0]["未核查"], 15)
        self.assertEqual(result["community"]["data"][0]["已核查"], 15)
        self.assertNotIn("已完成", result["inspector"]["columns"])

    def test_total_summary_projects_nested_tables_and_flat_compatibility_alias(self):
        community = sample_detailed_table()
        inspector = sample_detailed_table()
        inspector["columns"] = ["社区", "姓名", *inspector["columns"][1:]]
        inspector["data"][0]["姓名"] = "张三"
        inspector["data"][1]["姓名"] = "李四"
        payload = {
            "exists": True,
            "columns": community["columns"],
            "data": community["data"],
            "inspector": inspector,
            "community": community,
        }

        result = project_report_payload(payload, "two")

        self.assertNotIn("已完成", result["columns"])
        self.assertNotIn("已完成", result["inspector"]["columns"])
        self.assertNotIn("已完成", result["community"]["columns"])
        self.assertEqual(result["data"], result["community"]["data"])
        self.assertEqual(result["summary"], result["community"]["summary"])

    def test_total_row_sums_counts_and_recalculates_rates(self):
        result = project_report_payload(
            {"exists": True, **sample_detailed_table()},
            "three",
        )

        summary = result["summary"]
        self.assertEqual(summary["社区"], "总计")
        self.assertEqual(summary["数据总数"], 50)
        self.assertEqual(summary["未核查"], 14)
        self.assertEqual(summary["已核查"], 11)
        self.assertEqual(summary["已完成"], 25)
        self.assertEqual(summary["无法见底数"], 5)
        self.assertEqual(summary["核查完成率"], 0.5)
        self.assertEqual(summary["核查见底率"], 0.83)

    def test_total_reached_bottom_rate_is_zero_without_completed_data(self):
        table = sample_detailed_table()
        table["data"] = [
            {
                "社区": "长板",
                "数据总数": 5,
                "未核查": 3,
                "已核查": 2,
                "已完成": 0,
                "核查完成率": 0,
                "无法见底数": 0,
                "核查见底率": 0,
            }
        ]

        result = project_report_payload(
            {"exists": True, **table},
            "three",
        )

        self.assertEqual(result["summary"]["核查见底率"], 0.0)

    def test_two_column_mode_projects_total_row(self):
        result = project_report_payload(
            {"exists": True, **sample_detailed_table()},
            "two",
        )

        summary = result["summary"]
        self.assertEqual(summary["未核查"], 25)
        self.assertEqual(summary["已核查"], 25)
        self.assertNotIn("已完成", summary)
        self.assertEqual(summary["核查完成率"], 0.5)
        self.assertEqual(summary["核查见底率"], 0.83)

    def test_summary_report_total_recalculates_person_average(self):
        table = sample_detailed_table()
        table["columns"].extend(["网格员人数", "当日人均核查数"])
        table["data"][0].update({"网格员人数": 3, "当日人均核查数": 5})
        table["data"][1].update({"网格员人数": 1, "当日人均核查数": 10})

        result = project_report_payload({"exists": True, **table}, "three")

        self.assertEqual(result["summary"]["网格员人数"], 4)
        self.assertEqual(result["summary"]["每日人均核查数"], 6.25)
        self.assertNotIn("当日人均核查数", result["columns"])

    def test_range_average_uses_person_days_and_preserves_missing_value(self):
        table = sample_detailed_table()
        table["columns"].extend(["在岗人日", "每日人均核查数"])
        table["data"][0].update({"在岗人日": 3, "每日人均核查数": 5})
        table["data"][1].update({"在岗人日": 2, "每日人均核查数": 5})

        result = project_report_payload({"exists": True, **table}, "three")

        self.assertEqual(result["summary"]["在岗人日"], 5)
        self.assertEqual(result["summary"]["每日人均核查数"], 5)

        table["data"][1]["每日人均核查数"] = None
        result = project_report_payload({"exists": True, **table}, "three")
        self.assertIsNone(result["summary"]["每日人均核查数"])

    def test_inspector_total_uses_one_total_label(self):
        table = sample_detailed_table()
        table["columns"] = ["社区", "姓名", *table["columns"][1:]]
        table["data"][0]["姓名"] = "张三"
        table["data"][1]["姓名"] = "李四"
        payload = {
            "exists": True,
            "inspector": table,
            "community": sample_detailed_table(),
        }

        result = project_report_payload(payload, "three")

        self.assertEqual(result["inspector"]["summary"]["社区"], "总计")
        self.assertEqual(result["inspector"]["summary"]["姓名"], "")
        self.assertEqual(result["community"]["summary"]["社区"], "总计")

    async def test_report_endpoint_uses_saved_account_mode_by_default(self):
        with patch(
            "routers.stats.builder.get_report",
            new=AsyncMock(return_value=sample_report()),
        ):
            result = await get_report_endpoint(
                report_date="2026-07-28",
                parser_type="全链条",
                column_mode=None,
                user={"id": 7, "report_column_mode": "two"},
            )

        self.assertEqual(result["column_mode"], "two")
        self.assertNotIn("已完成", result["columns"])

    async def test_explicit_mode_can_be_reused_by_future_export(self):
        with patch(
            "routers.stats.builder.get_report",
            new=AsyncMock(return_value=sample_report()),
        ):
            result = await get_report_endpoint(
                report_date="2026-07-28",
                parser_type="全链条",
                column_mode="three",
                user={"id": 7, "report_column_mode": "two"},
            )

        self.assertEqual(result["column_mode"], "three")
        self.assertIn("已完成", result["columns"])

    async def test_user_preferences_are_saved_to_current_account(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor_context = MagicMock()
        cursor_context.__aenter__ = AsyncMock(return_value=cursor)
        cursor_context.__aexit__ = AsyncMock(return_value=None)
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=connection)
        pool.release = MagicMock()

        request = UserPreferencesRequest(
            table_display_mode="card",
            report_column_mode="two",
        )
        with patch("routers.auth.db_manager.get_pool", return_value=pool):
            result = await update_preferences(
                request,
                user={
                    "id": 7,
                    "username": "tester",
                    "role": "member",
                    "table_display_mode": "table",
                    "report_column_mode": "three",
                },
            )

        sql, params = cursor.execute.await_args.args
        self.assertIn("UPDATE _users", sql)
        self.assertEqual(params, ("card", "two", 7))
        self.assertEqual(result["user"]["table_display_mode"], "card")
        self.assertEqual(result["user"]["report_column_mode"], "two")

    def test_invalid_preference_value_is_rejected(self):
        with self.assertRaises(ValidationError):
            UserPreferencesRequest(
                table_display_mode="terminal",
                report_column_mode="two",
            )


if __name__ == "__main__":
    unittest.main()
