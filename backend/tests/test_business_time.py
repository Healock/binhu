from datetime import date, datetime, timezone
import unittest

from services.business_time import (
    business_date_range_utc_bounds,
    current_business_date,
)


class BusinessTimeTests(unittest.TestCase):
    def test_shanghai_early_morning_belongs_to_new_day(self):
        utc_time = datetime(2026, 7, 26, 18, 30, tzinfo=timezone.utc)

        self.assertEqual(
            current_business_date("Asia/Shanghai", utc_time),
            date(2026, 7, 27),
        )
        self.assertEqual(
            current_business_date("UTC", utc_time),
            date(2026, 7, 26),
        )

    def test_business_day_is_converted_to_utc_range(self):
        start, end = business_date_range_utc_bounds(
            "2026-07-27",
            "2026-07-27",
            "Asia/Shanghai",
        )

        self.assertEqual(start, datetime(2026, 7, 26, 16, 0))
        self.assertEqual(end, datetime(2026, 7, 27, 16, 0))

    def test_invalid_timezone_falls_back_to_shanghai(self):
        utc_time = datetime(2026, 7, 26, 18, 30, tzinfo=timezone.utc)

        self.assertEqual(
            current_business_date("Not/A-Timezone", utc_time),
            date(2026, 7, 27),
        )


if __name__ == "__main__":
    unittest.main()
