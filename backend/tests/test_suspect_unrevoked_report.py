import unittest

from services.report_builders import BUILDERS, IMPLEMENTED_TYPES
from services.report_builders.suspect_unrevoked import SuspectUnrevokedBuilder


class SuspectUnrevokedReportTests(unittest.TestCase):
    def setUp(self):
        self.builder = SuspectUnrevokedBuilder()

    def test_builder_is_registered(self):
        self.assertIsInstance(
            BUILDERS["疑似未注销模型三"],
            SuspectUnrevokedBuilder,
        )
        self.assertIn("疑似未注销模型三", IMPLEMENTED_TYPES)

    def test_workload_sql_uses_result_only_status(self):
        inspector_sql, community_sql = self.builder._build_workload_sql(
            "`2026-07-27_snapshot_suspectUnrevoked`",
            "`2026-07-26_snapshot_suspectUnrevoked`",
        )
        sql = " ".join((inspector_sql + community_sql).split())

        self.assertIn("t.`下发社区`", sql)
        self.assertNotIn("现住址", sql)
        self.assertIn("0,", sql)
        for result in ("近期返吴", "近期反吴", "在吴", "离吴"):
            self.assertIn(f"'{result}'", sql)
        self.assertIn(
            "TRIM(IFNULL(prev.`核查结果`, '')) "
            "<> TRIM(IFNULL(t.`核查结果`, ''))",
            sql,
        )
        self.assertEqual(
            sql.count("SUM(CASE WHEN NOT (TRIM(IFNULL(t.`核查结果`, ''))"),
            2,
        )

    def test_first_day_sql_filters_business_day(self):
        inspector_sql, _ = self.builder._build_workload_sql(
            "`2026-07-27_snapshot_suspectUnrevoked`",
            None,
        )

        self.assertIn(
            "t._last_updated_at >= %s AND t._last_updated_at < %s",
            inspector_sql,
        )

    def test_range_sql_maps_unresolved_and_completed(self):
        inspector_sql, community_sql = self.builder.build_stats_sql(
            "(`snapshot_union`)"
        )
        sql = " ".join((inspector_sql + community_sql).split())

        self.assertIn("t.`下发社区` AS 社区", sql)
        self.assertIn("0 AS 已核查", sql)
        self.assertIn("AS 未核查", sql)
        self.assertIn("AS 已完成", sql)
        self.assertEqual(sql.count("0 AS 无法见底数"), 2)
        self.assertIn("AS 核查见底率", sql)
        self.assertNotIn("现住址", sql)


if __name__ == "__main__":
    unittest.main()
