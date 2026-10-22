from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ProductionMaintenanceContractTests(unittest.TestCase):
    def test_workflow_has_registered_operations_and_fixed_identity_phase(self) -> None:
        workflow = (ROOT / ".github/workflows/production-maintenance.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("environment: production", workflow)
        self.assertIn("options: [measure, backup, apply, verify, rollback]", workflow)
        self.assertIn("internal-department-retirement", workflow)
        self.assertIn("photo-sheet-retirement", workflow)
        self.assertIn("registry-property-identity", workflow)
        self.assertIn("venue-photo-recovery", workflow)
        self.assertIn("old_property_id", workflow)
        self.assertIn("canonical_property_id", workflow)
        self.assertIn("measurement_hash", workflow)
        self.assertIn("INITIATED_BY: ${{ github.actor }}", workflow)
        self.assertIn("confirm-remove-internal", workflow)
        self.assertIn("confirm-empty-photo-tables", workflow)
        self.assertIn("confirm-registry-property-identity-rollback", workflow)
        self.assertNotIn('"root@$DEPLOY_HOST"', workflow)
        self.assertIn('"binhu-deploy@$DEPLOY_HOST"', workflow)

    def test_gateway_has_no_date_or_arbitrary_command_escape(self) -> None:
        gateway = (ROOT / "deploy/binhu-deploy-gateway").read_text(encoding="utf-8")
        self.assertIn("production-maintenance", gateway)
        self.assertIn("internal-department-retirement|photo-sheet-retirement|registry-property-identity", gateway)
        self.assertIn("measure|backup|apply|verify|rollback", gateway)
        maintenance_block = gateway.split(
            "if [[ \"${command_parts[0]:-}\" == \"production-maintenance\" ]]",
            1,
        )[1].split("# 生产当前任务维护", 1)[0]
        self.assertNotRegex(maintenance_block, re.compile(r"2026-09-14"))
        self.assertIn("confirm-remove-internal", gateway)
        self.assertIn("confirm-empty-photo-tables", gateway)
        self.assertIn("confirm-registry-property-identity", gateway)
        self.assertIn("venue-photo-recovery", gateway)
        self.assertIn("confirm-venue-photo-recovery", gateway)
        self.assertIn("valid_actor()", gateway)
        self.assertIn("^[0-9a-f]{64}$", gateway)
        self.assertNotIn("--sql", maintenance_block)
        self.assertNotIn("--table", maintenance_block)

    def test_root_contract_requires_evidence_and_backup_before_apply(self) -> None:
        script = (ROOT / "deploy/binhu-maintenance").read_text(encoding="utf-8")
        self.assertIn("/run/lock/binhu-maintenance.lock", script)
        self.assertIn("backup manifest is missing; run backup first", script)
        self.assertIn("measure count mismatch", script)
        self.assertIn("--backup-manifest", script)
        self.assertIn("--confirm-remove-internal", script)
        self.assertIn("--confirm-empty-photo-tables", script)
        self.assertIn("backup_database RegistryData", script)
        self.assertIn('"$maintenance_actor"', script)
        self.assertIn("registry_property_identity_maintenance migrate --apply", script)
        self.assertIn("registry_property_identity_maintenance rollback", script)
        self.assertIn('"${compose[@]}" stop --timeout 60 backend', script)
        self.assertIn('"${compose[@]}" run --name', script)
        self.assertIn('"${compose[@]}" start backend', script)
        self.assertIn("{{.State.Running}}' binhu-backend", script)
        self.assertIn("/api/health", script)
        self.assertIn("backup is not a mysqldump export", script)
        self.assertIn('"restore_procedure": "approved_manual_only"', script)
        self.assertIn("backend_restore_failed", script)
        self.assertIn("identity measurement evidence is immutable; use a new run id", script)
        self.assertIn("identity backup evidence is immutable; use a new run id", script)
        self.assertIn("registry-property-identity", script)
        self.assertIn("venue-photos.tar.gz", script)
        self.assertIn('"$backup_dir/RegistryData.sql.gz.sha256"', script)
        self.assertIn("photo recovery measurement evidence is immutable", script)

    def test_installer_requires_and_installs_root_maintenance_contract(self) -> None:
        installer = (ROOT / "deploy/install-server-deployer.sh").read_text(encoding="utf-8")
        self.assertIn('"$script_dir/binhu-maintenance"', installer)
        self.assertIn("/usr/local/sbin/binhu-maintenance", installer)


if __name__ == "__main__":
    unittest.main()
