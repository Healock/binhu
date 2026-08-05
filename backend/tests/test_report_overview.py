import unittest
from unittest.mock import AsyncMock, patch

from services import report_overview


class FakeCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeConnection:
    def cursor(self):
        return FakeCursor()


class FakePool:
    def __init__(self):
        self.connection = FakeConnection()
        self.released = False

    async def acquire(self):
        return self.connection

    def release(self, connection):
        self.released = connection is self.connection


class NewKeyCursor:
    def __init__(self):
        self.rows = []
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self.rows = [("new-task",)]

    async def fetchall(self):
        return list(self.rows)


class EffectiveTaskCursor:
    def __init__(self):
        self.call = None

    async def execute(self, sql, params=None):
        self.call = (" ".join(sql.split()), params)

    async def fetchall(self):
        return []


class OnlineOverviewTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_fullchain_snapshot_defaults_registration_to_blank(self):
        parser = report_overview.get_parser("全链条")
        columns = [column for column in parser.COLUMNS if column != "登记情况"]
        row = tuple(f"value-{index}" for index in range(len(columns)))

        values = report_overview._snapshot_values(parser, columns, row)

        self.assertEqual(values["登记情况"], "")
        self.assertEqual(values["创建时间"], row[columns.index("创建时间")])

    async def test_effective_tasks_use_fixed_registered_person_scope(self):
        cursor = EffectiveTaskCursor()

        await report_overview._load_effective_tasks(
            cursor,
            "2026-07-29",
            "2026-07-31",
            ["全链条"],
        )

        sql, _ = cursor.call
        self.assertIn("JOIN OnlineData._grid_members AS person", sql)
        self.assertIn("department.department_type='community'", sql)
        self.assertIn("person.position IN ('组长', '组员')", sql)

    async def test_effective_tasks_can_limit_to_linked_inspector(self):
        cursor = EffectiveTaskCursor()

        await report_overview._load_effective_tasks(
            cursor,
            "2026-08-03",
            "2026-08-03",
            ["全链条", "寄递业"],
            ["长板", "长板村"],
            "网格员甲",
        )

        sql, params = cursor.call
        self.assertIn(
            "LOWER(TRIM(latest.inspector))=LOWER(TRIM(%s))",
            sql,
        )
        self.assertEqual(params[-3:], ("长板", "长板村", "网格员甲"))

    async def test_classifies_carryover_new_and_changed_tasks(self):
        pool = FakePool()
        runs = [
            (
                "2026-07-29",
                "全链条",
                "2026-07-29_snapshot_fullChain",
                "2026-07-28_snapshot_fullChain",
            ),
        ]
        tasks = [
            (
                "全链条",
                "carry-task",
                "unchecked",
                "2026-07-29",
                "carryover",
            ),
            (
                "全链条",
                "new-task",
                "completed",
                "2026-07-29",
                "activity",
            ),
            (
                "全链条",
                "changed-task",
                "checked",
                "2026-07-29",
                "activity",
            ),
        ]

        with (
            patch.object(
                report_overview.db_manager,
                "get_pool",
                return_value=pool,
            ),
            patch.object(
                report_overview,
                "_load_available_range",
                new=AsyncMock(
                    return_value=("2026-07-26", "2026-07-29", 4)
                ),
            ),
            patch.object(
                report_overview,
                "_load_runs",
                new=AsyncMock(return_value=runs),
            ),
            patch.object(
                report_overview,
                "_load_effective_tasks",
                new=AsyncMock(return_value=tasks),
            ),
            patch.object(
                report_overview,
                "_find_new_activity_keys",
                new=AsyncMock(
                    return_value={("全链条", "new-task")}
                ),
            ),
        ):
            result = await report_overview.get_online_overview(
                "2026-07-29",
                "2026-07-29",
                "全链条",
            )

        self.assertTrue(result["exists"])
        self.assertEqual(result["total_tasks"], 3)
        self.assertEqual(result["carryover_tasks"], 1)
        self.assertEqual(result["new_tasks"], 1)
        self.assertEqual(result["changed_tasks"], 1)
        self.assertEqual(result["pending_tasks"], 2)
        self.assertEqual(result["completed_tasks"], 1)
        self.assertEqual(result["completion_rate"], 0.3333)
        self.assertEqual(result["available_data_days"], 4)
        self.assertEqual(result["selected_data_days"], 1)
        self.assertTrue(pool.released)

    def test_detail_categories_partition_and_match_state_totals(self):
        tasks = [
            ("全链条", "carry-task", "unchecked", "2026-08-04", "carryover"),
            ("全链条", "new-task", "completed", "2026-08-05", "activity"),
            ("全链条", "changed-task", "checked", "2026-08-05", "activity"),
        ]
        new_keys = {("全链条", "new-task")}

        change_total = sum(len(report_overview._filter_tasks_by_category(
            tasks, new_keys, category
        )) for category in ("carryover", "new", "changed"))
        self.assertEqual(change_total, len(tasks))
        self.assertEqual(
            len(report_overview._filter_tasks_by_category(tasks, new_keys, "pending")),
            2,
        )
        self.assertEqual(
            len(report_overview._filter_tasks_by_category(tasks, new_keys, "completed")),
            1,
        )

    async def test_overview_details_reuse_category_and_snapshot_values(self):
        pool = FakePool()
        runs = [(
            "2026-08-05",
            "全链条",
            "2026-08-05_snapshot_fullChain",
            "2026-08-04_snapshot_fullChain",
        )]
        tasks = [
            ("全链条", "new-task", "completed", "2026-08-05", "activity"),
            ("全链条", "changed-task", "checked", "2026-08-05", "activity"),
        ]
        metadata = {
            ("全链条", "changed-task"): {
                "report_date": "2026-08-05",
                "community": "长板",
                "inspector": "网格员甲",
                "state": "checked",
            },
        }
        snapshots = {
            ("全链条", "changed-task"): {
                "姓名": "张三",
                "身份证号": "TEST-ID",
                "电话号码": "TEST-PHONE",
                "地址": "原地址",
                "现住址": "长板一号",
                "核查结果": "无法核实",
            },
        }

        with (
            patch.object(report_overview.db_manager, "get_pool", return_value=pool),
            patch.object(
                report_overview,
                "_resolve_parser_types",
                new=AsyncMock(return_value=["全链条"]),
            ),
            patch.object(report_overview, "_load_runs", new=AsyncMock(return_value=runs)),
            patch.object(
                report_overview,
                "_resolve_communities",
                new=AsyncMock(return_value=["长板", "长板村"]),
            ),
            patch.object(
                report_overview,
                "_load_effective_tasks",
                new=AsyncMock(return_value=tasks),
            ),
            patch.object(
                report_overview,
                "_find_new_activity_keys",
                new=AsyncMock(return_value={("全链条", "new-task")}),
            ),
            patch.object(
                report_overview,
                "_load_latest_task_metadata",
                new=AsyncMock(return_value=metadata),
            ),
            patch.object(
                report_overview,
                "_load_snapshot_values",
                new=AsyncMock(return_value=snapshots),
            ),
        ):
            result = await report_overview.get_online_overview_details(
                "2026-08-05",
                "2026-08-05",
                "全链条",
                "changed",
                community=["长板"],
            )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["data"][0]["summary"]["title"], "张三")
        self.assertEqual(result["data"][0]["community"], "长板")
        self.assertEqual(result["data"][0]["reason"], "已有任务在所选区间内发生有效变化")
        self.assertTrue(pool.released)

    async def test_returns_available_range_when_selection_has_no_runs(self):
        pool = FakePool()
        with (
            patch.object(
                report_overview.db_manager,
                "get_pool",
                return_value=pool,
            ),
            patch.object(
                report_overview,
                "_load_available_range",
                new=AsyncMock(
                    return_value=("2026-07-26", "2026-07-29", 4)
                ),
            ),
            patch.object(
                report_overview,
                "_load_runs",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await report_overview.get_online_overview(
                "2026-07-20",
                "2026-07-20",
                "全链条",
            )

        self.assertFalse(result["exists"])
        self.assertEqual(result["available_start_date"], "2026-07-26")
        self.assertEqual(result["available_end_date"], "2026-07-29")
        self.assertEqual(result["total_tasks"], 0)

    async def test_new_activity_uses_previous_snapshot_presence(self):
        cursor = NewKeyCursor()
        tasks = [
            (
                "全链条",
                "new-task",
                "completed",
                "2026-07-29",
                "activity",
            ),
            (
                "全链条",
                "changed-task",
                "checked",
                "2026-07-29",
                "activity",
            ),
            (
                "全链条",
                "carry-task",
                "unchecked",
                "2026-07-29",
                "carryover",
            ),
        ]
        runs = [
            (
                "2026-07-29",
                "全链条",
                "2026-07-29_snapshot_fullChain",
                "2026-07-28_snapshot_fullChain",
            ),
        ]

        result = await report_overview._find_new_activity_keys(
            cursor,
            tasks,
            runs,
        )

        self.assertEqual(result, {("全链条", "new-task")})
        self.assertEqual(
            set(cursor.calls[0][1]),
            {"new-task", "changed-task"},
        )
        self.assertIn(
            "previous._row_key IS NULL",
            cursor.calls[0][0],
        )

    async def test_first_snapshot_activity_counts_as_new(self):
        cursor = NewKeyCursor()
        tasks = [
            (
                "寄递业",
                "first-task",
                "unchecked",
                "2026-07-26",
                "activity",
            ),
        ]
        runs = [
            (
                "2026-07-26",
                "寄递业",
                "2026-07-26_snapshot_deliveryIndustry",
                None,
            ),
        ]

        result = await report_overview._find_new_activity_keys(
            cursor,
            tasks,
            runs,
        )

        self.assertEqual(result, {("寄递业", "first-task")})
        self.assertEqual(cursor.calls, [])


if __name__ == "__main__":
    unittest.main()
