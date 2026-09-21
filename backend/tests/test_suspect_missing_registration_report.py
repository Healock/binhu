import unittest

from services.report_builders import BUILDERS, IMPLEMENTED_TYPES
from services.report_builders.suspect_missing_registration import SuspectMissingRegistrationBuilder


class SuspectMissingRegistrationReportTests(unittest.TestCase):
    def test_builder_is_registered(self):
        self.assertIsInstance(BUILDERS["疑似漏登记"], SuspectMissingRegistrationBuilder)
        self.assertIn("疑似漏登记", IMPLEMENTED_TYPES)

    def test_internal_transfer_is_checked_and_external_transfer_reaches_bottom(self):
        builder = BUILDERS["疑似漏登记"]
        self.assertIn("移交（所内）", builder.ledger_state_sql("t"))
        sql = builder.ledger_reached_bottom_sql("t")
        for value in ("已登记", "离苏", "无需登记", "移交（所外）"):
            self.assertIn(value, sql)


if __name__ == "__main__":
    unittest.main()
