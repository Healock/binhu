from datetime import date
import unittest

from services.grid_member_status import (
    active_member_sql,
    get_status_snapshot,
    validate_leave_period,
)


class GridMemberStatusTests(unittest.TestCase):
    def test_permanent_off_duty_overrides_leave(self):
        result = get_status_snapshot(
            "离岗",
            date(2026, 7, 1),
            date(2026, 7, 31),
            date(2026, 7, 15),
        )
        self.assertEqual(result["effective_status"], "离岗")
        self.assertEqual(result["status_detail"], "长期离岗")

    def test_leave_is_active_on_both_boundary_dates(self):
        for current in (date(2026, 7, 10), date(2026, 7, 12)):
            result = get_status_snapshot(
                "在岗",
                date(2026, 7, 10),
                date(2026, 7, 12),
                current,
            )
            self.assertEqual(result["effective_status"], "离岗")
            self.assertEqual(result["leave_state"], "active")

    def test_future_and_expired_leave_do_not_change_active_status(self):
        upcoming = get_status_snapshot(
            "在岗",
            date(2026, 8, 1),
            date(2026, 8, 3),
            date(2026, 7, 27),
        )
        expired = get_status_snapshot(
            "在岗",
            date(2026, 7, 1),
            date(2026, 7, 3),
            date(2026, 7, 27),
        )
        self.assertEqual(upcoming["effective_status"], "在岗")
        self.assertEqual(upcoming["leave_state"], "upcoming")
        self.assertEqual(expired["effective_status"], "在岗")
        self.assertEqual(expired["leave_state"], "expired")

    def test_leave_dates_must_be_complete_and_ordered(self):
        with self.assertRaises(ValueError):
            validate_leave_period(date(2026, 7, 10), None)
        with self.assertRaises(ValueError):
            validate_leave_period(date(2026, 7, 12), date(2026, 7, 10))
        validate_leave_period(date(2026, 7, 10), date(2026, 7, 12))

    def test_active_member_sql_uses_requested_date_and_alias(self):
        condition = active_member_sql("g")
        self.assertEqual(condition.count("%s"), 1)
        self.assertIn("g.status = '在岗'", condition)
        self.assertIn("%s BETWEEN g.leave_start_date AND g.leave_end_date", condition)


if __name__ == "__main__":
    unittest.main()
