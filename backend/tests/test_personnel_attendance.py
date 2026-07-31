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
    save_weekend_board,
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
                if "FROM OnlineData._system_config" in self.last_sql:
                    return ('["中队长"]',)
                return None

            async def fetchall(self):
                if "FROM _grid_members" in self.last_sql:
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
                if "FROM OnlineData._system_config" in self.last_sql:
                    return ('["组员"]',)
                return None

            async def fetchall(self):
                if "FROM _grid_members" in self.last_sql:
                    return [(1, "张三", "长板", "组员")]
                if (
                    "FROM _personnel_weekend_duty" in self.last_sql
                    and self.last_params == (date(2026, 7, 27),)
                ):
                    return [(1, None)]
                return []

        board = await get_weekend_board(Cursor(), date(2026, 7, 27))

        self.assertTrue(board["complete"])
        self.assertEqual(board["unassigned_count"], 0)
        self.assertIsNone(board["members"][0]["assignment"])
        self.assertTrue(board["members"][0]["recorded"])

    async def test_save_accepts_null_duty_but_requires_every_member(self):
        initial_board = {
            "members": [{
                "id": 1,
                "name": "张三",
                "community": "长板",
                "position": "组员",
                "unavailable_days": [],
                "exempt": False,
            }],
        }
        saved_board = {
            **initial_board,
            "complete": True,
            "unassigned_count": 0,
        }

        class Cursor:
            def __init__(self):
                self.rows = []

            async def executemany(self, _sql, rows):
                self.rows = list(rows)

        class CursorContext:
            def __init__(self, cursor):
                self.cursor = cursor

            async def __aenter__(self):
                return self.cursor

            async def __aexit__(self, *_args):
                return False

        class Connection:
            def __init__(self):
                self.test_cursor = Cursor()
                self.committed = False
                self.rolled_back = False

            async def begin(self):
                return None

            def cursor(self):
                return CursorContext(self.test_cursor)

            async def commit(self):
                self.committed = True

            async def rollback(self):
                self.rolled_back = True

        conn = Connection()
        board_loader = AsyncMock(side_effect=[initial_board, saved_board])
        with patch(
            "services.personnel_attendance.get_weekend_board",
            board_loader,
        ):
            result = await save_weekend_board(
                conn,
                requested_date=date(2026, 7, 27),
                raw_assignments={1: None},
                updated_by=9,
            )

        self.assertIs(result, saved_board)
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)
        self.assertEqual(len(conn.test_cursor.rows), 1)
        self.assertIsNone(conn.test_cursor.rows[0][2])

        missing_conn = Connection()
        with patch(
            "services.personnel_attendance.get_weekend_board",
            AsyncMock(return_value=initial_board),
        ):
            with self.assertRaisesRegex(ValueError, "全部备勤人员"):
                await save_weekend_board(
                    missing_conn,
                    requested_date=date(2026, 7, 27),
                    raw_assignments={},
                    updated_by=9,
                )
        self.assertTrue(missing_conn.rolled_back)


if __name__ == "__main__":
    unittest.main()
