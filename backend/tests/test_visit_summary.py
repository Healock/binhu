from datetime import date
import unittest

from fastapi import HTTPException

from routers.visits import summary as summary_endpoint
from services.visit_summary import (
    COMMUNITY_COLUMNS,
    INSPECTOR_COLUMNS,
    VISIT_CATEGORY_RENTAL,
    VISIT_CATEGORY_SELF_OWNED,
    _round_ratio,
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
        if "COUNT(DISTINCT" not in normalized:
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
        self.assertEqual(_round_ratio(1, 4), 0.3)
        self.assertEqual(_round_ratio(0, 0), 0.0)

    async def test_builds_inspector_community_and_recalculated_totals(self):
        connection = SummaryConnection(
            inspector_rows=[
                ("长板", "张三", 4, 1, 0, 0, 3),
                ("长板", "李四", 2, 0, 1, 1, 1),
            ],
            community_rows=[
                ("长板", 6, 2, 1, 1, 1, 4),
            ],
        )

        result = await get_visit_summary(
            connection,
            date(2026, 7, 1),
            date(2026, 7, 31),
            selected_positions={"组长", "组员"},
            known_positions={},
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
        for column in (
            "社区",
            "走访户数",
            "新增",
            "变更",
            "注销",
            "总变动数",
            "户均变动数",
            "星级评定数",
            "星级评定率",
        ):
            self.assertEqual(
                inspector_total[column],
                result["community"]["summary"][column],
            )
        community_total = result["community"]["summary"]
        self.assertEqual(community_total["人均走访户数"], 3.0)
        self.assertEqual(community_total["人均变动数"], 1.5)

        self.assertEqual(
            [params for _, params in connection.calls],
            [(date(2026, 7, 1), date(2026, 7, 31))],
        )

    async def test_empty_range_still_returns_zero_totals(self):
        result = await get_visit_summary(
            SummaryConnection([], []),
            date(2026, 7, 1),
            date(2026, 7, 1),
            selected_positions={"组长", "组员"},
            known_positions={},
        )

        self.assertEqual(result["inspector"]["data"], [])
        self.assertEqual(result["inspector"]["summary"]["走访户数"], 0)
        self.assertEqual(result["community"]["summary"]["星级评定率"], 0.0)
        self.assertEqual(result["community"]["summary"]["人均走访户数"], 0.0)

    async def test_total_counts_cross_community_member_once(self):
        connection = SummaryConnection(
            inspector_rows=[
                ("长板", "张三", 2, 0, 1, 0, 1),
                ("水秀", "张三", 3, 0, 2, 0, 2),
            ],
            community_rows=[
                ("长板", 2, 1, 0, 1, 0, 1),
                ("水秀", 3, 1, 0, 2, 0, 2),
            ],
        )

        result = await get_visit_summary(
            connection,
            date(2026, 7, 1),
            date(2026, 7, 31),
            selected_positions={"组长", "组员"},
            known_positions={},
        )

        visit_averages = {
            row["社区"]: row["人均走访户数"]
            for row in result["community"]["data"]
        }
        self.assertEqual(
            visit_averages,
            {"长板": 2.0, "水秀": 3.0},
        )
        self.assertEqual(
            result["community"]["summary"]["人均走访户数"],
            5.0,
        )
        self.assertEqual(
            result["community"]["summary"]["人均变动数"],
            3.0,
        )

    async def test_known_unselected_position_is_hidden_but_unknown_remains(self):
        connection = SummaryConnection(
            inspector_rows=[
                ("长板", "组员甲", 2, 0, 1, 0, 1),
                ("长板", "中队长乙", 5, 1, 1, 0, 2),
                ("水秀", "名册外人员", 3, 0, 0, 1, 1),
            ],
            community_rows=[],
        )

        result = await get_visit_summary(
            connection,
            date(2026, 7, 1),
            date(2026, 7, 31),
            selected_positions={"组长", "组员"},
            known_positions={
                "组员甲": "组员",
                "中队长乙": "中队长",
            },
        )

        self.assertEqual(
            [row["姓名"] for row in result["inspector"]["data"]],
            ["组员甲", "名册外人员"],
        )
        self.assertEqual(
            result["community"]["summary"]["走访户数"],
            5,
        )

    async def test_rental_excludes_self_owned_even_if_old_config_selected_it(self):
        result = await get_visit_summary(
            SummaryConnection(
                [
                    ("长板", "组员甲", 2, 0, 1, 0, 1),
                    ("长板", "自购房乙", 4, 1, 0, 0, 2),
                    ("水秀", "名册外人员", 3, 0, 0, 1, 1),
                ],
                [],
            ),
            date(2026, 7, 1),
            date(2026, 7, 31),
            category=VISIT_CATEGORY_RENTAL,
            selected_positions={"组长", "组员", "自购房"},
            known_positions={
                "组员甲": "组员",
                "自购房乙": "自购房",
            },
        )

        self.assertEqual(result["category_label"], "出租房")
        self.assertEqual(
            [row["姓名"] for row in result["inspector"]["data"]],
            ["组员甲", "名册外人员"],
        )

    async def test_self_owned_only_contains_known_self_owned_people(self):
        result = await get_visit_summary(
            SummaryConnection(
                [
                    ("长板", "组员甲", 2, 0, 1, 0, 1),
                    ("长板", "自购房乙", 4, 1, 0, 0, 2),
                    ("水秀", "名册外人员", 3, 0, 0, 1, 1),
                ],
                [],
            ),
            date(2026, 7, 1),
            date(2026, 7, 31),
            category=VISIT_CATEGORY_SELF_OWNED,
            selected_positions={"组长", "组员"},
            known_positions={
                "组员甲": "组员",
                "自购房乙": "自购房",
            },
        )

        self.assertEqual(result["category"], "self_owned")
        self.assertEqual(result["category_label"], "自购房")
        self.assertEqual(
            [row["姓名"] for row in result["inspector"]["data"]],
            ["自购房乙"],
        )
        self.assertEqual(
            result["community"]["summary"]["走访户数"],
            4,
        )

    async def test_rejects_unknown_summary_category(self):
        with self.assertRaisesRegex(ValueError, "不支持的走访汇总类型"):
            await get_visit_summary(
                SummaryConnection([], []),
                date(2026, 7, 1),
                date(2026, 7, 31),
                category="other",
                selected_positions={"组员"},
                known_positions={},
            )

    async def test_rejects_reversed_date_range(self):
        with self.assertRaises(HTTPException) as raised:
            await summary_endpoint(
                start_date=date(2026, 7, 2),
                end_date=date(2026, 7, 1),
                category="rental",
                conn=None,
            )

        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
