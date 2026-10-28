from __future__ import annotations

import re
import gzip
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE_CONTROL_PATH = ROOT / "deploy/binhu-production-service-control"
SERVICE_CONTROL_LOADER = importlib.machinery.SourceFileLoader(
    "binhu_production_service_control", str(SERVICE_CONTROL_PATH)
)
SERVICE_CONTROL_SPEC = importlib.util.spec_from_loader(
    SERVICE_CONTROL_LOADER.name, SERVICE_CONTROL_LOADER
)
SERVICE_CONTROL = importlib.util.module_from_spec(SERVICE_CONTROL_SPEC)
SERVICE_CONTROL_LOADER.exec_module(SERVICE_CONTROL)


class ProductionMaintenanceContractTests(unittest.TestCase):
    def test_workflow_has_registered_operations_and_fixed_identity_phase(self) -> None:
        workflow = (ROOT / ".github/workflows/production-maintenance.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("environment: production", workflow)
        self.assertIn("options: [measure, backup, apply, verify, rollback, execute]", workflow)
        self.assertIn("internal-department-retirement", workflow)
        self.assertIn("photo-sheet-retirement", workflow)
        self.assertIn("registry-property-identity", workflow)
        self.assertIn("venue-photo-recovery", workflow)
        self.assertIn("pause-production", workflow)
        self.assertIn("resume-production", workflow)
        self.assertIn("confirm-pause-production", workflow)
        self.assertIn("confirm-resume-production", workflow)
        self.assertIn("pause-production' && 150 || 30", workflow)
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
        self.assertIn("internal-department-retirement|photo-sheet-retirement|registry-property-identity|venue-photo-recovery|pause-production|resume-production", gateway)
        self.assertIn("measure|backup|apply|verify|rollback", gateway)
        self.assertIn('[[ "$maintenance_operation" == "pause-production" || "$maintenance_operation" == "resume-production" ]] || {', gateway)
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
        self.assertIn("confirm-pause-production", gateway)
        self.assertIn("confirm-resume-production", gateway)
        self.assertIn("valid_actor()", gateway)
        self.assertIn("^[0-9a-f]{64}$", gateway)
        self.assertNotIn("--sql", maintenance_block)
        self.assertNotIn("--table", maintenance_block)

    @unittest.skipIf(os.name == "nt", "gateway behavior test requires POSIX bash")
    def test_execute_phase_is_exclusive_to_pause_and_resume(self) -> None:
        gateway_path = ROOT / "deploy/binhu-deploy-gateway"

        def invoke(command: str) -> subprocess.CompletedProcess[str]:
            environment = os.environ.copy()
            environment["SSH_ORIGINAL_COMMAND"] = command
            return subprocess.run(
                ["bash", str(gateway_path)], capture_output=True, text=True, env=environment,
            )

        pause = invoke(
            "production-maintenance pause-production execute short confirm-pause-production"
        )
        self.assertEqual(pause.returncode, 64)
        self.assertIn("Invalid production-maintenance run id.", pause.stderr)
        self.assertNotIn("Invalid production-maintenance phase.", pause.stderr)

        non_service = invoke(
            "production-maintenance internal-department-retirement execute run-id-123"
        )
        self.assertEqual(non_service.returncode, 64)
        self.assertIn("Invalid production-maintenance phase.", non_service.stderr)

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
        self.assertIn('"content_check": "stream_scan_v1"', script)
        self.assertIn('stream.read(1024 * 1024)', script)
        self.assertIn("binhu-production-service-control", script)
        self.assertIn("export BINHU_PROJECT_DIR BINHU_MAINTENANCE_EVIDENCE_ROOT", script)

    def test_service_control_has_fixed_targets_and_no_docker_removal_contract(self) -> None:
        script = SERVICE_CONTROL_PATH.read_text(encoding="utf-8")
        self.assertIn('TARGETS = {"binhu-backend", "binhu-mysql"}', script)
        self.assertIn('self.compose("stop", "--timeout", "60", "backend")', script)
        self.assertIn('self.compose("stop", "--timeout", "60", "mysql")', script)
        self.assertIn('self.compose("start", "mysql")', script)
        self.assertIn('self.compose("start", "backend")', script)
        for prohibited in ("docker rm", "docker system prune", "volume rm", "down -v"):
            self.assertNotIn(prohibited, script)

    def test_backup_manifest_requires_all_eight_hash_valid_dumps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for database in SERVICE_CONTROL.DATABASES:
                path = root / f"{database}.sql.gz"
                with gzip.open(path, "wt", encoding="utf-8") as stream:
                    stream.write(
                        f"-- MySQL dump\n-- Current Database: `{database}`\nUSE `{database}`;\n"
                    )
                entries.append({"database": database, "filename": path.name,
                                "size_bytes": path.stat().st_size,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            (root / "backup-manifest.json").write_text(json.dumps({
                "schema": 1, "operation": "pause-production", "run_id": "pause-test-001",
                "database_scope": sorted(SERVICE_CONTROL.DATABASES), "databases": entries,
            }), encoding="utf-8")
            verified = SERVICE_CONTROL.verify_backup_manifest(root, "pause-test-001")
            self.assertEqual(verified["database_scope"], sorted(SERVICE_CONTROL.DATABASES))
            (root / "OnlineData.sql.gz").write_bytes(b"tampered")
            with self.assertRaisesRegex(SERVICE_CONTROL.ServiceControlError, "backup_database_invalid"):
                SERVICE_CONTROL.verify_backup_manifest(root, "pause-test-001")

    def test_service_control_rejects_unexpected_scope_and_environment(self) -> None:
        with self.assertRaisesRegex(SERVICE_CONTROL.ServiceControlError, "invalid_run_id"):
            SERVICE_CONTROL.validate_run_id("../production")
        self.assertTrue(SERVICE_CONTROL.probe_bootstrap({"environment": "production"}))
        self.assertFalse(SERVICE_CONTROL.probe_bootstrap({"environment": "staging"}))
        self.assertTrue(SERVICE_CONTROL.probe_bootstrap({"environment": "production", "token": "x"}))

    def test_only_production_target_container_state_may_change(self) -> None:
        before = [
            {"id": "app", "name": "binhu-backend", "state": "running"},
            {"id": "db", "name": "binhu-mysql", "state": "running"},
            {"id": "frp", "name": "frpc", "state": "running"},
            {"id": "stg", "name": "binhu-staging-backend-1", "state": "running"},
        ]
        after = [dict(row, state="exited") if row["name"] in SERVICE_CONTROL.TARGETS else row for row in before]
        self.assertTrue(SERVICE_CONTROL.compare_protected_states(before, after))
        after[-1] = dict(after[-1], state="exited")
        self.assertFalse(SERVICE_CONTROL.compare_protected_states(before, after))

    def test_pause_and_resume_execute_fixed_order_with_protected_containers_unchanged(self) -> None:
        class FakeServiceControl(SERVICE_CONTROL.ProductionServiceControl):
            def __init__(self, evidence_root: Path, backup_root: Path):
                super().__init__(evidence_root=evidence_root, backup_root=backup_root)
                self.states = {"binhu-backend": "running", "binhu-mysql": "running"}
                self.calls: list[tuple[str, ...]] = []

            def acquire_lock(self):
                return object()

            def inspect_container(self, name: str) -> dict:
                service = SERVICE_CONTROL.EXPECTED_SERVICES[name]
                running = self.states[name] == "running"
                return {"id": name, "name": name, "project": "binhu", "service": service,
                        "state": self.states[name], "running": running,
                        "health": "healthy" if name == "binhu-mysql" and running else None}

            def require_services(self, expected_running: bool) -> dict:
                result = {name: self.inspect_container(name) for name in sorted(SERVICE_CONTROL.TARGETS)}
                if any(item["running"] is not expected_running for item in result.values()):
                    raise SERVICE_CONTROL.ServiceControlError("production_service_state_unexpected")
                return result

            def container_states(self) -> list[dict]:
                rows = [{"id": name, "name": name, "state": state} for name, state in self.states.items()]
                rows.extend([
                    {"id": "frp-id", "name": "frpc", "state": "running"},
                    {"id": "staging-id", "name": "binhu-staging-backend-1", "state": "running"},
                ])
                return sorted(rows, key=lambda row: row["name"])

            def probe_application(self) -> dict:
                return {"health_status": 200, "bootstrap_status": 200,
                        "bootstrap_environment_matches": True, "query_status": 401}

            def backup_database(self, backup_dir: Path, database: str) -> dict:
                path = backup_dir / f"{database}.sql.gz"
                with gzip.open(path, "wt", encoding="utf-8") as stream:
                    stream.write(
                        f"-- MySQL dump\n-- Current Database: `{database}`\nUSE `{database}`;\n"
                    )
                return {"database": database, "filename": path.name,
                        "size_bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

            def compose(self, *args: str) -> None:
                self.calls.append(args)
                action, service = args[0], args[-1]
                container = "binhu-backend" if service == "backend" else "binhu-mysql"
                self.states[container] = "exited" if action == "stop" else "running"

            def wait_mysql_healthy(self, timeout_seconds: int = 180) -> dict:
                return self.inspect_container("binhu-mysql")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = FakeServiceControl(root / "evidence", root / "backups")
            paused = controller.pause("pause-test-001", "confirm-pause-production")
            self.assertTrue(paused["accepted"])
            self.assertEqual(paused["backup_databases"], sorted(SERVICE_CONTROL.DATABASES))
            self.assertEqual(controller.calls[:2], [
                ("stop", "--timeout", "60", "backend"),
                ("stop", "--timeout", "60", "mysql"),
            ])
            resumed = controller.resume(
                "resume-test-001", "pause-test-001", "confirm-resume-production"
            )
            self.assertTrue(resumed["accepted"])
            self.assertEqual(resumed["pause_run_id"], "pause-test-001")
            self.assertEqual(controller.calls[2:], [("start", "mysql"), ("start", "backend")])
            self.assertTrue(resumed["protected_container_states_unchanged"])

    def test_installer_requires_and_installs_root_maintenance_contract(self) -> None:
        installer = (ROOT / "deploy/install-server-deployer.sh").read_text(encoding="utf-8")
        self.assertIn('"$script_dir/binhu-maintenance"', installer)
        self.assertIn("/usr/local/sbin/binhu-maintenance", installer)
        self.assertIn('"$script_dir/binhu-production-service-control"', installer)
        self.assertIn("/usr/local/libexec/binhu-production-service-control", installer)


if __name__ == "__main__":
    unittest.main()
