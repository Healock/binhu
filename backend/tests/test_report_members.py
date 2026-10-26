import os
import sqlite3
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
from services.personnel_positions import get_eligible_online_personnel


class ReportMemberCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_connection_reads_attendance_from_selected_personnel_schema(self):
        from datetime import date
        from config import settings
        from services.personnel_attendance import get_attendance_context
        from services.personnel_positions import get_known_personnel_positions

        # Preserve schema qualifiers: the report connection has no personnel
        # tables, and only the selected domain is attached. This checks read
        # routing, not MySQL locking or cross-schema write transactions.
        for active, schema in ((False, "OnlineFixture"), (True, "PlatformFixture")):
            with self.subTest(platform_active=active), sqlite3.connect(
                ":memory:", detect_types=sqlite3.PARSE_DECLTYPES
            ) as conn, patch.object(settings, "PLATFORM_DOMAIN_ACTIVE", active), patch.object(
                settings, "MYSQL_ONLINE_DATA_DB", "OnlineFixture"
            ), patch.object(
                settings, "MYSQL_PLATFORM_DB", "PlatformFixture"
            ):
                self.addCleanup(conn.close)
                conn.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
                conn.executescript(f"""
                    CREATE TABLE {schema}._grid_members(
                        id INT,name TEXT,position TEXT,status TEXT,
                        leave_start_date DATE,leave_end_date DATE
                    );
                    INSERT INTO {schema}._grid_members
                    VALUES(1,'合成人员','组员','在岗',NULL,NULL);
                    CREATE TABLE {schema}._grid_member_department_links(member_id INT,department_id INT,sort_order INT);
                    INSERT INTO {schema}._grid_member_department_links VALUES(1,2,0);
                    CREATE TABLE {schema}._departments(id INT,community_id INT,department_type TEXT);
                    INSERT INTO {schema}._departments VALUES(2,3,'community');
                    CREATE TABLE {schema}._communities(id INT,name TEXT);
                    INSERT INTO {schema}._communities VALUES(3,'合成社区');
                    CREATE TABLE {schema}._personnel_attendance_history(id INT,member_id INT,absence_type TEXT,start_date DATE,end_date DATE,reason TEXT,source TEXT,is_active INT);
                    INSERT INTO {schema}._personnel_attendance_history VALUES(1,1,'temporary_leave','2026-10-20','2026-10-20','','manual',1);
                    CREATE TABLE {schema}._personnel_work_schedule(member_id INT,work_date DATE,state TEXT);
                    INSERT INTO {schema}._personnel_work_schedule VALUES(1,'2026-10-19','rest');
                    CREATE TABLE {schema}._system_config(config_key TEXT,config_value TEXT);
                    INSERT INTO {schema}._system_config VALUES('attendance_daily_schedule_v1','1'),('attendance_history_started_on','2026-01-01'),('weekend_duty_positions','["组员"]');
                """)

                class ReportCursor:
                    async def execute(self, sql, params=()):
                        self.raw = conn.execute(sql.replace("%s", "?"), params)

                    async def fetchall(self):
                        return self.raw.fetchall()

                    async def fetchone(self):
                        return self.raw.fetchone()

                cur = ReportCursor()
                self.assertEqual(
                    await get_active_members(cur, "2026-10-19"),
                    [("合成社区", "合成人员")],
                )
                self.assertEqual(await get_active_members(cur, "2026-10-20"), [])
                self.assertEqual(await get_active_members(cur, "2026-10-21"), [("合成社区", "合成人员")])
                context = await get_attendance_context(
                    cur, start_date=date(2026, 10, 21), end_date=date(2026, 10, 21),
                    selected_positions={"组员"}, community_scope=["合成社区"], member_names=["合成人员"],
                )
                self.assertEqual(list(context["members"]), ["合成人员"])
                self.assertEqual((await get_known_personnel_positions(cur))["合成人员"], "组员")
                self.assertEqual((await get_eligible_online_personnel(cur))["合成人员"]["community"], "合成社区")

    async def test_online_scope_requires_registered_group_member_and_community(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[("张三", "组员", "长板")])

        result = await get_eligible_online_personnel(cursor)

        sql = " ".join(cursor.execute.await_args.args[0].split())
        self.assertIn("department.department_type='community'", sql)
        self.assertIn("member.position IN ('组长', '组员')", sql)
        self.assertEqual(result["张三"]["community"], "长板")

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

    async def test_active_member_count_is_not_filtered_by_weekend_schedule(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[("合成社区", "合成人员")])

        result = await get_active_members(cursor, "2026-10-25")

        sql, params = cursor.execute.await_args.args
        self.assertIn("department.department_type='community'", sql)
        self.assertIn("g.position IN ('组长', '组员')", sql)
        self.assertNotIn("_personnel_work_schedule", sql)
        self.assertEqual(params, ("2026-10-25",) * 3)
        self.assertEqual(result, [("合成社区", "合成人员")])

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
            "services.report_members.get_eligible_online_personnel",
            new=AsyncMock(return_value={
                "张三": {"position": "组员", "community": "社区甲"},
                "后来请假的人员": {"position": "组员", "community": "社区丙"},
            }),
        ), patch(
            "services.report_members.get_active_members",
            new=AsyncMock(return_value=[("社区乙", "李四")]),
        ):
            completed = await complete_inspector_rows(
                cursor,
                existing,
                "2026-07-28",
            )

        self.assertEqual(len(completed), 2)
        self.assertIn(existing[0], completed)
        self.assertNotIn(existing[1], completed)
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

    async def test_only_registered_eligible_people_remain(self):
        cursor = MagicMock()
        existing = [
            ("社区甲", "组员甲", 1, 0, 0, 1, 1, 0, 1),
            ("社区乙", "中队长乙", 2, 0, 0, 2, 1, 0, 1),
            ("社区外", "名册外人员", 3, 0, 0, 3, 1, 0, 1),
        ]

        with patch(
            "services.report_members.get_eligible_online_personnel",
            new=AsyncMock(return_value={
                "组员甲": {"position": "组员", "community": "社区甲"},
            }),
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
        self.assertNotIn(existing[2], completed)

    async def test_community_rebuild_filters_without_deleting_person_rows(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=('["组长", "组员"]',))

        await rebuild_community_report_table(
            cursor,
            "`2026-07-28_daily_fullChain_inspector`",
            "`2026-07-28_daily_fullChain_community`",
        )

        sql = cursor.execute.await_args.args[0]
        normalized = " ".join(sql.split())
        self.assertIn("JOIN OnlineData._grid_members", normalized)
        self.assertIn("department.department_type='community'", normalized)
        self.assertIn("person.position IN ('组长', '组员')", normalized)
        self.assertIn(
            "SUM(report_row.已完成) / (SUM(report_row.已完成) "
            "+ SUM(report_row.无法见底数))",
            normalized,
        )
        self.assertNotIn("DELETE", normalized)

    async def test_ledger_community_rebuild_only_keeps_eligible_inspectors(self):
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
        self.assertIn("JOIN OnlineData._grid_members", normalized)
        self.assertIn("department.department_type='community'", normalized)
        self.assertIn("person.position IN ('组长', '组员')", normalized)
        self.assertIn(
            "SUM(ledger.task_state = 'completed') "
            "/ (SUM(ledger.task_state = 'completed') "
            "+ SUM(ledger.unable_to_verify))",
            normalized,
        )
        self.assertEqual(
            params,
            ("2026-07-29", "寄递业"),
        )
        zero_sql = cursor.execute.await_args_list[-1].args[0]
        normalized_zero_sql = " ".join(zero_sql.split())
        self.assertIn("report_row.数据总数 = 0", normalized_zero_sql)
        self.assertIn("existing.社区 IS NULL", normalized_zero_sql)

    def test_summary_keeps_first_community_when_member_moves_or_has_multiple_departments(self):
        rows = [("合成原社区", "合成核查员", 1, 0, 0, 1, 1, 0, 1),
                ("合成另一社区", "合成核查员", 2, 0, 0, 2, 1, 0, 1)]
        merged = merge_inspector_rows(
            rows, [("合成现社区", "合成核查员")], preserve_community=True,
        )
        self.assertEqual({r[0]: r[2] for r in merged}, {"合成原社区": 1, "合成另一社区": 2})

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
                ("名册社区", "张三", 14, 3, 4, 7, 0.5, 2, 0.78),
                ("社区乙", "李四", 0, 0, 0, 0, 0.0, 0, 0.0),
                ("社区外", "名册外人员", 3, 0, 1, 2, 0.67, 0, 1.0),
            ],
        )


if __name__ == "__main__":
    unittest.main()
