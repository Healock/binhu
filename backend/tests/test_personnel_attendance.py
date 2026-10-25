from datetime import date
from decimal import Decimal
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from deps import require_admin
from routers.personnel_attendance import (
    WeekendAssignment,
    WeekendDutyUpdate,
    read_attendance_status,
    router as attendance_router,
)
from services.personnel_attendance import (
    allocate_person_days,
    get_weekend_board,
    is_member_on_duty,
    normalize_week_start,
    period_covers,
    required_week_starts,
    save_weekend_board,
    weekend_dates,
)
from services.attendance_schedule import table


def context(*, periods=None, duties=None, missing=()):
    member = {
        "id": 1,
        "name": "张三",
        "community": "长板",
        "position": "组员",
    }
    return {
        "members": {"张三": member},
        "periods": periods or {},
        "daily": {(m,day):'work' for (m,_),day in (duties or {}).items() if day is not None},
        "missing_week_starts": set(missing),
        "history_started_on": date(2026, 7, 30),
        "legacy_history_incomplete": False,
    }


class PersonnelAttendanceTests(unittest.TestCase):
    def test_weekend_save_requires_attendance_permission(self):
        protected_paths = {
            route.path
            for route in attendance_router.routes
            if any(
                dependency.call.__name__ == "require_attendance_manage"
                for dependency in route.dependant.dependencies
            )
        }
        self.assertIn(
            "/api/personnel/attendance/weekend-duty",
            protected_paths,
        )
        get_route = next(
            route
            for route in attendance_router.routes
            if route.path == "/api/personnel/attendance/weekend-duty"
            and "GET" in route.methods
        )
        self.assertFalse(any(
            dependency.call.__name__ == "require_attendance_manage"
            for dependency in get_route.dependant.dependencies
        ))
        status_route = next(
            route
            for route in attendance_router.routes
            if route.path == "/api/personnel/attendance/status"
        )
        self.assertFalse(any(
            dependency.call.__name__ == "require_attendance_manage"
            for dependency in status_route.dependant.dependencies
        ))

    def test_week_is_always_normalized_to_monday(self):
        self.assertEqual(
            normalize_week_start(date(2026, 8, 2)),
            date(2026, 7, 27),
        )
        self.assertEqual(
            weekend_dates(date(2026, 7, 29)),
            (date(2026, 8, 1), date(2026, 8, 2)),
        )

    def test_missing_schedule_only_checks_weekends_reached_by_today(self):
        self.assertEqual(
            required_week_starts(
                date(2026, 8, 1),
                date(2026, 8, 16),
                date(2026, 8, 1),
            ),
            [date(2026, 7, 27)],
        )
        self.assertEqual(
            required_week_starts(
                date(2026, 8, 2),
                date(2026, 8, 16),
                date(2026, 8, 2),
            ),
            [date(2026, 7, 27)],
        )

    def test_weekend_payload_rejects_duplicate_people(self):
        with self.assertRaises(ValidationError):
            WeekendDutyUpdate(
                week_start=date(2026, 7, 27),
                request_id="request-0000000001",
                assignments=[
                    WeekendAssignment(member_id=1, duty_days=["saturday"],expected_version=0),
                    WeekendAssignment(member_id=1, duty_days=["sunday"],expected_version=0),
                ],
            )

    def test_absence_period_includes_both_boundaries(self):
        periods = [{
            "start_date": date(2026, 8, 1),
            "end_date": date(2026, 8, 2),
            "is_active": True,
        }]
        self.assertTrue(period_covers(date(2026, 8, 1), periods))
        self.assertTrue(period_covers(date(2026, 8, 2), periods))
        self.assertFalse(period_covers(date(2026, 8, 3), periods))

    def test_weekday_is_on_duty_and_leave_overrides_it(self):
        member = context()["members"]["张三"]
        self.assertTrue(
            is_member_on_duty(member, date(2026, 7, 31), context())
        )
        leave_context = context(periods={
            1: [{
                "start_date": date(2026, 7, 31),
                "end_date": date(2026, 7, 31),
                "is_active": True,
            }]
        })
        self.assertFalse(
            is_member_on_duty(
                member,
                date(2026, 7, 31),
                leave_context,
            )
        )

    def test_weekend_only_counts_the_assigned_day(self):
        week_start = date(2026, 7, 27)
        duty_context = context(
            duties={(1, week_start): date(2026, 8, 2)},
        )
        member = duty_context["members"]["张三"]
        self.assertFalse(
            is_member_on_duty(member, date(2026, 8, 1), duty_context)
        )
        self.assertTrue(
            is_member_on_duty(member, date(2026, 8, 2), duty_context)
        )

    def test_cross_community_day_is_split_by_actual_visit_count(self):
        result = allocate_person_days(
            start_date=date(2026, 7, 31),
            end_date=date(2026, 7, 31),
            daily_visits={
                (date(2026, 7, 31), "张三"): {
                    "长板": 1,
                    "水秀": 3,
                },
            },
            context=context(),
            include_unknown=True,
        )
        self.assertEqual(
            result["community_person_days"]["长板"],
            Decimal("0.25"),
        )
        self.assertEqual(
            result["community_person_days"]["水秀"],
            Decimal("0.75"),
        )
        self.assertEqual(result["total_person_days"], Decimal("1.00"))

    def test_multi_community_member_counts_once_globally_and_once_per_community(self):
        multi_context = context()
        multi_context["members"]["张三"]["communities"] = ["长板", "阅湖"]
        result = allocate_person_days(
            start_date=date(2026, 7, 31),
            end_date=date(2026, 7, 31),
            daily_visits={},
            context=multi_context,
            include_unknown=True,
        )

        self.assertEqual(result["community_person_days"]["长板"], Decimal("1"))
        self.assertEqual(result["community_person_days"]["阅湖"], Decimal("1"))
        self.assertEqual(result["total_person_days"], Decimal("1"))

    def test_real_visit_on_rest_day_is_reported_but_not_counted(self):
        result = allocate_person_days(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            daily_visits={
                (date(2026, 8, 1), "张三"): {"长板": 2},
            },
            context=context(),
            include_unknown=True,
        )
        self.assertEqual(result["total_person_days"], Decimal("0"))
        self.assertEqual(result["worked_while_off"], 1)

    def test_visit_during_leave_is_reported_separately_and_not_counted(self):
        leave_context = context(periods={
            1: [{
                "start_date": date(2026, 8, 1),
                "end_date": date(2026, 8, 1),
                "absence_type": "temporary_leave",
                "is_active": True,
            }]
        })
        result = allocate_person_days(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            daily_visits={(date(2026, 8, 1), "张三"): {"长板": 2}},
            context=leave_context,
            include_unknown=True,
        )
        self.assertEqual(result["total_person_days"], Decimal("0"))
        self.assertEqual(result["worked_while_off"], 0)
        self.assertEqual(result["worked_while_leave"], 1)

    def test_range_with_weekend_visits_uses_schedule_person_days(self):
        start = date(2026, 10, 23)
        end = date(2026, 10, 25)
        members = {
            name: {
                "id": index,
                "name": name,
                "community": "江城",
                "communities": ["江城"],
                "position": "组员",
            }
            for index, name in enumerate(("甲", "乙", "丙"), start=1)
        }
        daily = {
            (member_id, date(2026, 10, 24)): "work"
            for member_id in (1, 2, 3)
        }
        result = allocate_person_days(
            start_date=start,
            end_date=end,
            daily_visits={
                (date(2026, 10, 23), "甲"): {"江城": 1},
                (date(2026, 10, 23), "丙"): {"江城": 1},
                (date(2026, 10, 24), "甲"): {"江城": 1},
                (date(2026, 10, 25), "乙"): {"江城": 1},
                (date(2026, 10, 25), "丙"): {"江城": 1},
            },
            context={
                "members": members,
                "periods": {
                    2: [{
                        "start_date": date(2026, 10, 23),
                        "end_date": date(2026, 10, 23),
                        "absence_type": "temporary_leave",
                        "is_active": True,
                    }],
                },
                "daily": daily,
                "weekend_duty_positions": {"组员"},
                "missing_week_starts": set(),
                "history_started_on": date(2026, 1, 1),
                "legacy_history_incomplete": False,
            },
            include_unknown=False,
        )
        self.assertEqual(result["total_person_days"], Decimal("5"))
        self.assertEqual(result["community_person_days"]["江城"], Decimal("5"))
        self.assertEqual(result["worked_while_off"], 2)
        self.assertEqual(result["worked_while_leave"], 0)

    def test_missing_weekend_schedule_marks_average_incomplete(self):
        result = allocate_person_days(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            daily_visits={},
            context=context(missing={date(2026, 7, 27)}),
            include_unknown=True,
        )
        self.assertFalse(result["complete"])
        self.assertEqual(
            result["missing_week_starts"],
            [date(2026, 7, 27)],
        )


class PersonnelAttendanceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_status_rejects_invalid_range_before_query(self):
        with self.assertRaises(HTTPException) as raised:
            await read_attendance_status(
                start_date=date(2026, 8, 2),
                end_date=date(2026, 8, 1),
                conn=None,
            )
        self.assertEqual(raised.exception.status_code, 400)

    async def test_weekend_board_uses_configured_positions(self):
        class Cursor:
            def __init__(self):
                self.last_sql = ""
                self.last_params = None

            async def execute(self, sql, params=None):
                self.last_sql = " ".join(str(sql).split())
                self.last_params = params

            async def fetchone(self):
                if f"FROM {table('_system_config')}" in self.last_sql:
                    return ('["中队长"]',)
                return None

            async def fetchall(self):
                if f"FROM {table('_grid_members')} AS member" in self.last_sql:
                    self.assert_configured_position_query()
                    return [(1, "队长甲", "长板", "中队长")]
                return []

            def assert_configured_position_query(self):
                if self.last_params != ["中队长"]:
                    raise AssertionError(
                        f"unexpected position params: {self.last_params!r}"
                    )

        board = await get_weekend_board(Cursor(), date(2026, 7, 27))

        self.assertEqual(
            [member["position"] for member in board["members"]],
            ["中队长"],
        )
        self.assertEqual(board["positions"], ["中队长"])
        self.assertEqual(board["unassigned_count"], 1)

    async def test_saved_null_duty_means_two_rest_days_and_is_complete(self):
        class Cursor:
            def __init__(self):
                self.last_sql = ""
                self.last_params = None

            async def execute(self, sql, params=None):
                self.last_sql = " ".join(str(sql).split())
                self.last_params = params

            async def fetchone(self):
                if f"FROM {table('_system_config')}" in self.last_sql:
                    return ('["组员"]',)
                return None

            async def fetchall(self):
                if f"FROM {table('_grid_members')} AS member" in self.last_sql:
                    return [(1, "张三", "长板", "组员")]
                if (
                    "FROM _personnel_weekend_duty" in self.last_sql
                    and self.last_params == (date(2026, 7, 27),)
                ):
                    return [(1, None)]
                return []

        with patch('services.personnel_attendance.load_daily',AsyncMock(return_value={(1,date(2026,8,1)):'rest',(1,date(2026,8,2)):'rest'})):
            board = await get_weekend_board(Cursor(), date(2026, 7, 27))

        self.assertTrue(board["complete"])
        self.assertEqual(board["unassigned_count"], 0)
        self.assertEqual(board["members"][0]["duty_days"],[])
        self.assertTrue(board["members"][0]["recorded"])

    async def test_single_day_legacy_payload_is_rejected(self):
        with self.assertRaises(ValidationError):
            WeekendAssignment(member_id=1,duty_day='saturday')


if __name__ == "__main__":
    unittest.main()
