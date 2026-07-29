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


class OnlineOverviewTests(unittest.IsolatedAsyncioTestCase):
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
