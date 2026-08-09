from datetime import date
from decimal import Decimal
import unittest

from fastapi import HTTPException

from routers.visits import summary as summary_endpoint
from services.visit_summary import (
    COMMUNITY_COLUMNS,
    INSPECTOR_COLUMNS,
    VISIT_CATEGORY_RENTAL,
    VISIT_CATEGORY_SELF_OWNED,
    _balanced_person_day_display,
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
        if "FROM OnlineData._grid_members g" in normalized:
            self.rows = list(self.connection.active_member_rows)
        else:
            self.rows = list(self.connection.inspector_rows)

    async def fetchall(self):
        return list(self.rows)


class SummaryConnection:
    def __init__(self, inspector_rows, community_rows=(), active_member_rows=()):
        self.inspector_rows = inspector_rows
        self.active_member_rows = active_member_rows
        self.calls = []

    def cursor(self):
        return SummaryCursor(self)


def attendance_context(members=None):
    return {
        "members": members or {},
        "periods": {},
        "duties": {},
        "missing_week_starts": set(),
        "history_started_on": date(2026, 1, 1),
        "legacy_history_incomplete": False,
    }


class VisitSummaryTests(unittest.IsolatedAsyncioTestCase):
    def test_household_average_uses_round_half_up(self):
        self.assertEqual(_round_ratio(1, 4), 0.3)
        self.assertEqual(_round_ratio(0, 0), 0.0)

    def test_person_day_display_keeps_community_sum_equal_to_total(self):
        displayed = _balanced_person_day_display({
            "长板": Decimal("0.333333"),
            "水秀": Decimal("0.333333"),
            "冬梅": Decimal("0.333334"),
        })
        self.assertEqual(sum(displayed.values()), Decimal("1.0"))

    async def test_builds_inspector_community_and_recalculated_totals(self):
        connection = SummaryConnection(
            inspector_rows=[
                (date(2026, 7, 1), "长板", "张三", 4, 1, 0, 0, 3),
                (date(2026, 7, 1), "长板", "李四", 2, 0, 1, 1, 1),
            ],
            active_member_rows=[("长板", "张三"), ("长板", "李四")],
        )

        result = await get_visit_summary(
            connection,
            date(2026, 7, 1),
            date(2026, 7, 31),
            selected_positions={"组长", "组员"},
            known_positions={},
            attendance_context=attendance_context(),
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
        self.assertEqual(community_total["人均日走访户数"], 3.0)
        self.assertEqual(community_total["人均日变动数"], 1.5)
        self.assertEqual(community_total["在岗人日"], 2.0)
        self.assertEqual(community_total["网格员人数"], 2)
        self.assertEqual(
            result["community"]["data"][0]["网格员人数"],
            2,
        )
        self.assertEqual(
            result["overview"],
            {
                "visit_records": 6,
                "participant_count": 2,
                "person_days": 2.0,
                "community_count": 1,
                "added_count": 1,
                "changed_count": 1,
                "cancelled_count": 1,
                "total_changes": 3,
                "rated_records": 4,
                "unrated_records": 2,
                "rating_rate": 0.6667,
            },
        )

        self.assertEqual(
            connection.calls[0][1],
            (date(2026, 7, 1), date(2026, 7, 31)),
        )
        self.assertEqual(connection.calls[-1][1], ("2026-07-31",))

    async def test_empty_range_still_returns_zero_totals(self):
        result = await get_visit_summary(
            SummaryConnection([], []),
            date(2026, 7, 1),
            date(2026, 7, 1),
            selected_positions={"组长", "组员"},
            known_positions={},
            attendance_context=attendance_context(),
        )

        self.assertEqual(result["inspector"]["data"], [])
        self.assertEqual(result["inspector"]["summary"]["走访户数"], 0)
        self.assertEqual(result["community"]["summary"]["星级评定率"], 0.0)
        self.assertEqual(result["community"]["summary"]["人均日走访户数"], 0.0)
        self.assertEqual(result["overview"]["visit_records"], 0)
        self.assertEqual(result["overview"]["unrated_records"], 0)
        self.assertEqual(result["overview"]["rating_rate"], 0.0)

    async def test_missing_weekend_roster_hides_per_day_averages(self):
        incomplete = attendance_context()
        incomplete["missing_week_starts"] = {date(2026, 7, 27)}
        result = await get_visit_summary(
            SummaryConnection([
                (date(2026, 8, 1), "长板", "张三", 2, 0, 1, 0, 1),
            ]),
            date(2026, 8, 1),
            date(2026, 8, 2),
            selected_positions={"组长", "组员"},
            known_positions={},
            attendance_context=incomplete,
        )

        self.assertFalse(result["attendance"]["complete"])
        self.assertEqual(
            result["attendance"]["missing_week_starts"],
            ["2026-07-27"],
        )
        self.assertIsNone(
            result["community"]["summary"]["人均日走访户数"]
        )
        self.assertIsNone(
            result["community"]["summary"]["人均日变动数"]
        )

    async def test_total_counts_cross_community_member_once(self):
        connection = SummaryConnection(
            inspector_rows=[
                (date(2026, 7, 1), "长板", "张三", 2, 0, 1, 0, 1),
                (date(2026, 7, 1), "水秀", "张三", 3, 0, 2, 0, 2),
            ],
        )

        result = await get_visit_summary(
            connection,
            date(2026, 7, 1),
            date(2026, 7, 31),
            selected_positions={"组长", "组员"},
            known_positions={},
            attendance_context=attendance_context(),
        )

        visit_averages = {
            row["社区"]: row["人均日走访户数"]
            for row in result["community"]["data"]
        }
        self.assertEqual(
            visit_averages,
            {"长板": 5.0, "水秀": 5.0},
        )
        self.assertEqual(
            result["community"]["summary"]["人均日走访户数"],
            5.0,
        )
        self.assertEqual(
            result["community"]["summary"]["人均日变动数"],
            3.0,
        )

    async def test_known_unselected_position_is_hidden_but_unknown_remains(self):
        connection = SummaryConnection(
            inspector_rows=[
                (date(2026, 7, 1), "长板", "组员甲", 2, 0, 1, 0, 1),
                (date(2026, 7, 1), "长板", "中队长乙", 5, 1, 1, 0, 2),
                (date(2026, 7, 1), "水秀", "名册外人员", 3, 0, 0, 1, 1),
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
            attendance_context=attendance_context(),
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
                    (date(2026, 7, 1), "长板", "组员甲", 2, 0, 1, 0, 1),
                    (date(2026, 7, 1), "长板", "自购房乙", 4, 1, 0, 0, 2),
                    (date(2026, 7, 1), "水秀", "名册外人员", 3, 0, 0, 1, 1),
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
            attendance_context=attendance_context(),
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
                    (date(2026, 7, 1), "长板", "组员甲", 2, 0, 1, 0, 1),
                    (date(2026, 7, 1), "长板", "自购房乙", 4, 1, 0, 0, 2),
                    (date(2026, 7, 1), "水秀", "名册外人员", 3, 0, 0, 1, 1),
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
            attendance_context=attendance_context({
                "自购房乙": {
                    "id": 1,
                    "name": "自购房乙",
                    "community": "长板",
                    "position": "自购房",
                },
            }),
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
                attendance_context=attendance_context(),
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
