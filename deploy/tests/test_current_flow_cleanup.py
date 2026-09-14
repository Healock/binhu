import ast
import unittest
from datetime import date
from pathlib import Path

from backend.migrations.current_flow_cleanup import parse_business_date, should_archive_current_flow


ROOT = Path(__file__).resolve().parents[2]


class CurrentFlowCleanupContractTests(unittest.TestCase):
    def test_business_date_parser_accepts_confirmed_formats_only(self):
        expected = date(2026, 9, 14)
        for value in (
            "2026-09-14",
            "2026/9/14",
            "2026/09/14",
            "2026-09-14 08:30:00",
            "2026年9月14日",
        ):
            self.assertEqual(parse_business_date(value), expected)
        self.assertIsNone(parse_business_date("2026-09-14 备注"))
        self.assertIsNone(parse_business_date("2026-02-30"))
        self.assertIsNone(parse_business_date(""))

    def test_gateway_contract_is_fixed_to_approved_date_and_parser(self):
        source = (ROOT / "deploy/binhu-deploy-gateway").read_text(encoding="utf-8")
        self.assertIn("current-flow-cleanup", source)
        self.assertIn('"$cleanup_business_date" == "2026-09-14"', source)

    def test_tool_has_no_raw_business_value_output(self):
        tree = ast.parse(
            (ROOT / "backend/migrations/current_flow_cleanup.py").read_text(encoding="utf-8")
        )
        source = ast.unparse(tree)
        self.assertNotIn("print(values", source)
        self.assertIn("digest_row", source)

    def test_current_flow_cleanup_archives_all_live_rows_but_keeps_date_gate_for_summary(self):
        self.assertTrue(should_archive_current_flow({"下发日期": "2026-09-04"}))
        self.assertTrue(should_archive_current_flow({"下发日期": "2026-09-14"}))
        self.assertTrue(should_archive_current_flow({"下发日期": ""}))


if __name__ == "__main__":
    unittest.main()
