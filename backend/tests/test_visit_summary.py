from datetime import date
import unittest

from fastapi import HTTPException

from routers.visits import summary as summary_endpoint
from services.visit_summary import (
    COMMUNITY_COLUMNS,
    INSPECTOR_COLUMNS,
    _average_per_household,
    get_visit_summary,
)


class SummaryCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.connection.calls.append((normalized, params))
        if "TRIM(`操作人`)" in normalized:
            self.rows = list(self.connection.inspector_rows)
        else:
            self.rows = list(self.connection.community_rows)

    async def fetchall(self):
        return list(self.rows)


class SummaryConnection:
    def __init__(self, inspector_rows, community_rows):
        self.inspector_rows = inspector_rows
        self.community_rows = community_rows
        self.calls = []

    def cursor(self):
        return SummaryCursor(self)


class VisitSummaryTests(unittest.IsolatedAsyncioTestCase):
    def test_household_average_uses_round_half_up(self):
        self.assertEqual(_average_per_household(1, 4), 0.3)
        self.assertEqual(_average_per_household(0, 0), 0.0)

    async def test_builds_inspector_community_and_recalculated_totals(self):
        connection = SummaryConnection(
            inspector_rows=[
                ("长板", "张三", 4, 1, 0, 0, 3),
                ("长板", "李四", 2, 0, 1, 1, 1),
            ],
            community_rows=[
                ("长板", 6, 1, 1, 1, 4),
            ],
        )

        result = await get_visit_summary(
            connection,
            date(2026, 7, 1),
            date(2026, 7, 31),
        )

        self.assertEqual(result["inspector"]["columns"], INSPECTOR_COLUMNS)
        self.assertEqual(result["community"]["columns"], COMMUNITY_COLUMNS)
        first = result["inspector"]["data"][0]
        self.assertEqual(first["总变动数"], 1)
        self.assertEqual(first["户均变动数"], 0.3)
        self.assertEqual(first["星级评定数"], 3)
        self.assertEqual(first["星级评定率"], 0.75)

        inspector_total = result["inspector"]["summary"]
        self.assertEqual(inspector_total["社区"], "总计")
        self.assertEqual(inspector_total["走访户数"], 6)
        self.assertEqual(inspector_total["总变动数"], 3)
        self.assertEqual(inspector_total["户均变动数"], 0.5)
        self.assertEqual(inspector_total["星级评定率"], 0.6667)
        for column in COMMUNITY_COLUMNS:
            self.assertEqual(
                inspector_total[column],
                result["community"]["summary"][column],
            )

        self.assertEqual(
            [params for _, params in connection.calls],
            [
                (date(2026, 7, 1), date(2026, 7, 31)),
                (date(2026, 7, 1), date(2026, 7, 31)),
            ],
        )

    async def test_empty_range_still_returns_zero_totals(self):
        result = await get_visit_summary(
            SummaryConnection([], []),
            date(2026, 7, 1),
            date(2026, 7, 1),
        )

        self.assertEqual(result["inspector"]["data"], [])
        self.assertEqual(result["inspector"]["summary"]["走访户数"], 0)
        self.assertEqual(result["community"]["summary"]["星级评定率"], 0.0)

    async def test_rejects_reversed_date_range(self):
        with self.assertRaises(HTTPException) as raised:
            await summary_endpoint(
                start_date=date(2026, 7, 2),
                end_date=date(2026, 7, 1),
                conn=None,
            )

        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
