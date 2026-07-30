from datetime import date
from decimal import Decimal
import os
import unittest

from pydantic import ValidationError

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from deps import require_admin
from routers.personnel_attendance import (
    WeekendAssignment,
    WeekendDutyUpdate,
    router as attendance_router,
)
from services.personnel_attendance import (
    allocate_person_days,
    is_member_on_duty,
    normalize_week_start,
    period_covers,
    weekend_dates,
)


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
        "duties": duties or {},
        "missing_week_starts": set(missing),
        "history_started_on": date(2026, 7, 30),
        "legacy_history_incomplete": False,
    }


class PersonnelAttendanceTests(unittest.TestCase):
    def test_weekend_save_requires_admin_but_board_is_readable(self):
        protected_paths = {
            route.path
            for route in attendance_router.routes
            if any(
                dependency.call is require_admin
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
            dependency.call is require_admin
            for dependency in get_route.dependant.dependencies
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

    def test_weekend_payload_rejects_duplicate_people(self):
        with self.assertRaises(ValidationError):
            WeekendDutyUpdate(
                week_start=date(2026, 7, 27),
                assignments=[
                    WeekendAssignment(member_id=1, duty_day="saturday"),
                    WeekendAssignment(member_id=1, duty_day="sunday"),
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

    def test_real_visit_overrides_rest_day_but_is_reported(self):
        result = allocate_person_days(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            daily_visits={
                (date(2026, 8, 1), "张三"): {"长板": 2},
            },
            context=context(),
            include_unknown=True,
        )
        self.assertEqual(result["total_person_days"], Decimal("1"))
        self.assertEqual(result["worked_while_off"], 1)

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


if __name__ == "__main__":
    unittest.main()
