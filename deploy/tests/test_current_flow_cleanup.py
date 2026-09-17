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

    def test_backend_exposes_automated_backup_manifest_read_only(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(
            "${BINHU_DEPLOY_BACKUP_DIR:-./deploy-backups/automated}:/root/binhu/deploy-backups/automated:ro",
            compose,
        )

    def test_cleanup_evidence_is_persisted_outside_the_backend_container(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(
            "${BINHU_CLEANUP_EVIDENCE_DIR:-./deploy-evidence/current-flow-cleanup}:/srv/binhu-release-evidence-current-flow-cleanup",
            compose,
        )
        gateway = (ROOT / "deploy/binhu-deploy").read_text(encoding="utf-8")
        self.assertIn(
            "BINHU_CLEANUP_EVIDENCE_ROOT=/srv/binhu-release-evidence-current-flow-cleanup",
            gateway,
        )


if __name__ == "__main__":
    unittest.main()
