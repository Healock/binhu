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
