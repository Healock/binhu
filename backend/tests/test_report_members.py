import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

try:
    import aiomysql  # noqa: F401
except ModuleNotFoundError:
    aiomysql_stub = types.ModuleType("aiomysql")
    aiomysql_stub.Pool = object
    sys.modules["aiomysql"] = aiomysql_stub

from services.report_members import (
    canonical_community,
    canonicalize_community_rows,
    canonicalize_inspector_rows,
    complete_inspector_rows,
    get_active_members,
    get_missing_zero_rows,
    insert_zero_member_rows,
    merge_inspector_rows,
    rebuild_community_report_from_ledger,
    rebuild_community_report_table,
)


class ReportMemberCompletionTests(unittest.IsolatedAsyncioTestCase):
    def test_community_alias_combines_historical_report_rows(self):
        aliases = {"南厍": "南厍", "南厍村": "南厍"}
        inspector_rows = canonicalize_inspector_rows(
            [
                ("南厍村", "张三", 5, 0, 0, 5, 1, 0, 1),
                ("南厍", "张三", 1, 1, 0, 0, 0, 0, 0),
            ],
            aliases,
        )
        community_rows = canonicalize_community_rows(
            [
                ("南厍村", 5, 0, 0, 5, 1, 0, 1),
                ("南厍", 1, 1, 0, 0, 0, 0, 0),
            ],
            aliases,
        )

        self.assertEqual(canonical_community(" 南厍村 ", aliases), "南厍")
        self.assertEqual(
            inspector_rows,
            [("南厍", "张三", 6, 1, 0, 5, 0.83, 0, 1.0)],
        )
        self.assertEqual(
            community_rows,
            [("南厍", 6, 1, 0, 5, 0.83, 0, 1.0)],
        )

    def test_missing_active_member_gets_zero_row_without_duplicates(self):
        existing = [
            ("业务社区", "张三", 2, 0, 0, 2, 1, 0, 1),
            ("社区外", "名册外人员", 1, 0, 1, 0, 0, 0, 1),
        ]
        active_members = [
            ("名册社区", " 张三 "),
            ("社区乙", "李四"),
            ("社区乙", "李四"),
        ]

        missing = get_missing_zero_rows(existing, active_members)

        self.assertEqual(
            missing,
            [("社区乙", "李四", 0, 0, 0, 0, 0, 0, 0)],
        )

    async def test_active_member_query_uses_requested_date_and_status_rules(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(
            return_value=[("", "王五"), ("社区甲", "赵六")]
        )

        members = await get_active_members(
            cursor,
            "2026-07-28",
            ["组长", "组员"],
        )

        sql, params = cursor.execute.await_args.args
        self.assertIn("g.status = '在岗'", sql)
        self.assertIn(
            "%s BETWEEN g.leave_start_date AND g.leave_end_date",
            sql,
        )
        self.assertIn("g.position IN (%s, %s)", sql)
        self.assertEqual(params, ("组长", "组员", "2026-07-28"))
        self.assertEqual(
            members,
            [("未分配社区", "王五"), ("社区甲", "赵六")],
        )

    async def test_old_report_is_completed_without_losing_real_rows(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[])
        existing = [
            ("社区甲", "张三", 1, 0, 0, 1, 1, 0, 1),
            ("社区外", "名册外人员", 1, 1, 0, 0, 0, 1, 0),
            ("社区丙", "后来请假的人员", 0, 0, 0, 0, 0, 0, 0),
        ]

        with patch(
            "services.report_members.get_configured_positions",
            new=AsyncMock(return_value=["组长", "组员"]),
        ), patch(
            "services.report_members.get_known_personnel_positions",
            new=AsyncMock(
                return_value={
                    "张三": "组员",
                    "后来请假的人员": "组员",
                    "不参与统计的人": "中队长",
                }
            ),
        ), patch(
            "services.report_members.get_active_members",
            new=AsyncMock(return_value=[("社区乙", "李四")]),
        ):
            completed = await complete_inspector_rows(
                cursor,
                existing,
                "2026-07-28",
            )

        self.assertEqual(len(completed), 3)
        self.assertIn(existing[0], completed)
        self.assertIn(existing[1], completed)
        self.assertIn(
            ("社区乙", "李四", 0, 0, 0, 0, 0, 0, 0),
            completed,
        )
        self.assertNotIn(existing[2], completed)

    async def test_new_report_persists_missing_zero_rows(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[("社区甲", "张三")])
        cursor.executemany = AsyncMock()

        with patch(
            "services.report_members.get_active_members",
            new=AsyncMock(
                return_value=[
                    ("社区甲", "张三"),
                    ("社区乙", "李四"),
                ]
            ),
        ):
            inserted = await insert_zero_member_rows(
                cursor,
                "`2026-07-28_daily_fullChain_inspector`",
                "2026-07-28",
            )

        self.assertEqual(inserted, 1)
        rows = cursor.executemany.await_args.args[1]
        self.assertEqual(
            rows,
            [("社区乙", "李四", 0, 0, 0, 0, 0, 0, 0)],
        )

    async def test_known_unselected_person_is_hidden_but_unknown_person_remains(self):
        cursor = MagicMock()
        existing = [
            ("社区甲", "组员甲", 1, 0, 0, 1, 1, 0, 1),
            ("社区乙", "中队长乙", 2, 0, 0, 2, 1, 0, 1),
            ("社区外", "名册外人员", 3, 0, 0, 3, 1, 0, 1),
        ]

        with patch(
            "services.report_members.get_configured_positions",
            new=AsyncMock(return_value=["组长", "组员"]),
        ), patch(
            "services.report_members.get_known_personnel_positions",
            new=AsyncMock(
                return_value={
                    "组员甲": "组员",
                    "中队长乙": "中队长",
                }
            ),
        ), patch(
            "services.report_members.get_active_members",
            new=AsyncMock(return_value=[]),
        ):
            completed = await complete_inspector_rows(
                cursor,
                existing,
                "2026-07-28",
            )

        self.assertIn(existing[0], completed)
        self.assertNotIn(existing[1], completed)
        self.assertIn(existing[2], completed)

    async def test_community_rebuild_filters_without_deleting_person_rows(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=('["组长", "组员"]',))

        await rebuild_community_report_table(
            cursor,
            "`2026-07-28_daily_fullChain_inspector`",
            "`2026-07-28_daily_fullChain_community`",
        )

        sql, params = cursor.execute.await_args.args
        normalized = " ".join(sql.split())
        self.assertIn("LEFT JOIN OnlineData._grid_members", normalized)
        self.assertIn("person.id IS NULL", normalized)
        self.assertIn("person.position IN (%s, %s)", normalized)
        self.assertIn(
            "GREATEST( SUM(report_row.已完成) "
            "- SUM(report_row.无法见底数), 0 ) "
            "/ SUM(report_row.已完成)",
            normalized,
        )
        self.assertNotIn("DELETE", normalized)
        self.assertEqual(params, ["组长", "组员"])

    async def test_ledger_community_rebuild_keeps_rows_without_inspector(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=('["组长", "组员"]',))

        await rebuild_community_report_from_ledger(
            cursor,
            "`2026-07-29_daily_deliveryIndustry_inspector`",
            "`2026-07-29_daily_deliveryIndustry_community`",
            "2026-07-29",
            "寄递业",
        )

        ledger_call = cursor.execute.await_args_list[-2]
        sql, params = ledger_call.args
        normalized = " ".join(sql.split())
        self.assertIn("FROM _daily_task_ledger AS ledger", normalized)
        self.assertIn(
            "LEFT JOIN OnlineData._community_aliases",
            normalized,
        )
        self.assertIn(
            "COALESCE(formal_community.name, ledger.community)",
            normalized,
        )
        self.assertNotIn("ledger.inspector <>", normalized)
        self.assertIn("person.id IS NULL", normalized)
        self.assertIn("person.position IN (%s, %s)", normalized)
        self.assertIn(
            "SUM(ledger.reached_bottom) "
            "/ SUM(ledger.task_state = 'completed')",
            normalized,
        )
        self.assertEqual(
            params,
            ("2026-07-29", "寄递业", "组长", "组员"),
        )
        zero_sql, zero_params = cursor.execute.await_args_list[-1].args
        normalized_zero_sql = " ".join(zero_sql.split())
        self.assertIn("report_row.数据总数 = 0", normalized_zero_sql)
        self.assertIn("existing.社区 IS NULL", normalized_zero_sql)
        self.assertEqual(zero_params, ["组长", "组员"])

    def test_total_summary_merges_same_person_across_business_tables(self):
        rows = [
            ("业务社区甲", "张三", 10, 2, 3, 5, 0.5, 1, 0.4),
            ("业务社区乙", " 张三 ", 4, 1, 1, 2, 0.5, 1, 0.25),
            ("社区乙", "李四", 0, 0, 0, 0, 0, 0, 0),
            ("社区乙", "李四", 0, 0, 0, 0, 0, 0, 0),
            ("社区外", "名册外人员", 3, 0, 1, 2, 0.67, 0, 0.67),
        ]

        merged = merge_inspector_rows(
            rows,
            [("名册社区", "张三"), ("社区乙", "李四")],
        )

        self.assertEqual(
            merged,
            [
                ("名册社区", "张三", 14, 3, 4, 7, 0.5, 2, 0.71),
                ("社区乙", "李四", 0, 0, 0, 0, 0.0, 0, 0.0),
                ("社区外", "名册外人员", 3, 0, 1, 2, 0.67, 0, 1.0),
            ],
        )


if __name__ == "__main__":
    unittest.main()
