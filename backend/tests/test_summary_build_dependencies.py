import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.report_builders.summary import build_summary_with_subreports


class SummaryBuildDependenciesTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_configured_subreports_before_summary(self):
        events = []
        fullchain = MagicMock()
        rental = MagicMock()

        async def build_fullchain(date_str):
            events.append(("subreport", "全链条", date_str))
            return {"date": date_str, "inspector_rows": 10, "community_rows": 2}

        async def build_rental(date_str):
            events.append(("subreport", "出租房屋核查", date_str))
            return {"date": date_str, "inspector_rows": 8, "community_rows": 2}

        async def build_total(date_str, summary_types=None):
            events.append(("summary", "总汇总表", date_str))
            self.assertEqual(summary_types, ["全链条", "出租房屋核查"])
            return {
                "implemented": True,
                "date": date_str,
                "type": "总汇总表",
                "rows": 2,
            }

        fullchain.build = AsyncMock(side_effect=build_fullchain)
        rental.build = AsyncMock(side_effect=build_rental)

        with patch(
            "services.report_builders.summary._load_summary_types",
            new=AsyncMock(return_value=["全链条", "出租房屋核查"]),
        ), patch(
            "services.report_builders.summary.BUILDERS",
            {"全链条": fullchain, "出租房屋核查": rental},
        ), patch(
            "services.report_builders.summary.build_summary",
            new=AsyncMock(side_effect=build_total),
        ):
            result = await build_summary_with_subreports("2026-07-27")

        self.assertEqual(
            events,
            [
                ("subreport", "全链条", "2026-07-27"),
                ("subreport", "出租房屋核查", "2026-07-27"),
                ("summary", "总汇总表", "2026-07-27"),
            ],
        )
        self.assertTrue(result["implemented"])
        self.assertEqual(result["rows"], 2)
        self.assertEqual(
            [item["parser_type"] for item in result["subreports"]],
            ["全链条", "出租房屋核查"],
        )

    async def test_stops_before_summary_when_subreport_has_no_snapshot(self):
        fullchain = MagicMock()
        rental = MagicMock()
        fullchain.build = AsyncMock(
            return_value={
                "date": "2026-07-27",
                "inspector_rows": 10,
                "community_rows": 2,
            }
        )
        rental.build = AsyncMock(
            return_value={
                "implemented": False,
                "message": "2026-07-27 没有同步快照，不能生成日报",
            }
        )
        build_total = AsyncMock()

        with patch(
            "services.report_builders.summary._load_summary_types",
            new=AsyncMock(return_value=["全链条", "出租房屋核查"]),
        ), patch(
            "services.report_builders.summary.BUILDERS",
            {"全链条": fullchain, "出租房屋核查": rental},
        ), patch(
            "services.report_builders.summary.build_summary",
            new=build_total,
        ):
            result = await build_summary_with_subreports("2026-07-27")

        self.assertFalse(result["implemented"])
        self.assertEqual(result["failed_type"], "出租房屋核查")
        self.assertIn("出租房屋核查", result["message"])
        self.assertEqual(len(result["subreports"]), 1)
        build_total.assert_not_awaited()

    async def test_stops_before_summary_when_subreport_raises(self):
        fullchain = MagicMock()
        fullchain.build = AsyncMock(side_effect=RuntimeError("database unavailable"))
        build_total = AsyncMock()

        with patch(
            "services.report_builders.summary._load_summary_types",
            new=AsyncMock(return_value=["全链条"]),
        ), patch(
            "services.report_builders.summary.BUILDERS",
            {"全链条": fullchain},
        ), patch(
            "services.report_builders.summary.build_summary",
            new=build_total,
        ), patch("traceback.print_exc"):
            result = await build_summary_with_subreports("2026-07-27")

        self.assertFalse(result["implemented"])
        self.assertEqual(result["failed_type"], "全链条")
        self.assertIn("查看服务器日志", result["message"])
        build_total.assert_not_awaited()


class SummaryBuildRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_endpoint_uses_dependency_build(self):
        from routers.stats import build_report

        result_value = {
            "implemented": True,
            "date": "2026-07-27",
            "type": "总汇总表",
            "rows": 13,
            "subreports": [{"parser_type": "全链条"}],
        }

        with patch(
            "routers.stats.build_summary_with_subreports",
            new=AsyncMock(return_value=result_value),
        ) as dependency_build:
            result = await build_report("2026-07-27", "总汇总表")

        dependency_build.assert_awaited_once_with("2026-07-27")
        self.assertEqual(result["message"], "分汇总表和总汇总表生成成功")
        self.assertEqual(result["rows"], 13)
        self.assertEqual(len(result["subreports"]), 1)


if __name__ == "__main__":
    unittest.main()
