import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.environments import staging_migration as migration


ARTIFACT_ID = "b" * 64
IMAGE_ID = "sha256:" + "d" * 64
COMMIT = "c" * 40
DEV_RUN = "dev-update-" + "e" * 16
RUN_ID = "staging-migrate-" + "a" * 16
CANDIDATE_RUN = "staging-app-" + "f" * 16
MEASURED = {
    module: {"command": "measure", "consistent": False}
    for module in migration.MIGRATION_MODULES
}


def candidate_manifest():
    return {
        "environment": "staging", "state": "measured", "run_id": CANDIDATE_RUN,
        "artifact_id": ARTIFACT_ID, "image_id": IMAGE_ID, "commit": COMMIT,
        "version": "0.32.2", "dev_acceptance_run_id": DEV_RUN,
    }


class StagingMigrationTests(unittest.TestCase):
    def test_contract_is_staging_only_and_has_fixed_modules(self):
        source = (Path(__file__).parents[1] / "environments" / "staging_migration.py").read_text(encoding="utf-8")
        wrapper = (Path(__file__).parents[1] / "environments" / "binhu-staging-application-gateway").read_text(encoding="utf-8")
        self.assertIn('manifest.get("environment") != "staging"', source)
        self.assertIn('staging-migrate-[0-9a-f]{16}', wrapper)
        self.assertIn('^(measure|apply|verify)$', wrapper)
        self.assertIn('migrations.task_command_read_model', source)
        self.assertIn('migrations.task_view_state_revisions', source)
        self.assertNotIn("init_db()", source)

    def test_candidate_migration_does_not_require_current_staging_artifact_match(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            candidates = base / "application" / "candidates"
            root = candidates / CANDIDATE_RUN
            root.mkdir(parents=True)
            (root / "promotion.json").write_text(json.dumps(candidate_manifest()), encoding="utf-8")
            (root / "artifact").mkdir()
            (root / "image").mkdir()
            dev_root = base / "dev"
            dev_manifest = {"environment": "development", "artifact_id": ARTIFACT_ID, "commit": COMMIT,
                            "version": "0.32.2", "images": {"backend": IMAGE_ID}}
            dev_report_dir = base / DEV_RUN
            dev_report_dir.mkdir()
            (dev_report_dir / "result.json").write_text(json.dumps({
                "environment": "development", "health": True, "artifact_id": ARTIFACT_ID,
                "image_id": IMAGE_ID, "commit": COMMIT, "version": "0.32.2",
            }), encoding="utf-8")
            with patch.object(migration, "APPLICATION_ROOT", base / "application"), \
                 patch.object(migration, "EVIDENCE_ROOT", base), \
                 patch.object(migration, "root_for", return_value=dev_root), \
                 patch.object(migration, "read_configuration", return_value=(
                     dev_manifest, {"name": "binhu-development"}, "APP_ENVIRONMENT=development\n")), \
                 patch.object(migration, "verify_artifact", return_value={
                     "artifact_id": ARTIFACT_ID, "commit": COMMIT, "version": "0.32.2"}), \
                 patch.object(migration, "verify_image", return_value={"image_id": IMAGE_ID}):
                selected, manifest, image = migration._candidate(ARTIFACT_ID)
            self.assertEqual(selected, root)
            self.assertEqual(manifest["run_id"], CANDIDATE_RUN)
            self.assertEqual(image["image_id"], IMAGE_ID)

    def test_candidate_rejects_dev_configuration_with_wrong_environment_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            candidates = base / "application" / "candidates"
            root = candidates / CANDIDATE_RUN
            root.mkdir(parents=True)
            (root / "promotion.json").write_text(json.dumps(candidate_manifest()), encoding="utf-8")
            (root / "artifact").mkdir()
            (root / "image").mkdir()
            dev_manifest = {"environment": "staging", "artifact_id": ARTIFACT_ID,
                            "commit": COMMIT, "version": "0.32.2",
                            "images": {"backend": IMAGE_ID}}
            dev_report_dir = base / DEV_RUN
            dev_report_dir.mkdir()
            (dev_report_dir / "result.json").write_text(json.dumps({
                "environment": "development", "health": True, "artifact_id": ARTIFACT_ID,
                "image_id": IMAGE_ID, "commit": COMMIT, "version": "0.32.2",
            }), encoding="utf-8")
            with patch.object(migration, "APPLICATION_ROOT", base / "application"), \
                 patch.object(migration, "EVIDENCE_ROOT", base), \
                 patch.object(migration, "root_for", return_value=base / "dev"), \
                 patch.object(migration, "read_configuration", return_value=(
                     dev_manifest, {"name": "binhu-development"},
                     "APP_ENVIRONMENT=development\n")), \
                 patch.object(migration, "verify_artifact", return_value={
                     "artifact_id": ARTIFACT_ID, "commit": COMMIT, "version": "0.32.2"}), \
                 patch.object(migration, "verify_image", return_value={"image_id": IMAGE_ID}):
                with self.assertRaisesRegex(ValueError, "dev_acceptance_invalid"):
                    migration._candidate(ARTIFACT_ID)

    def test_measure_records_candidate_results_in_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / RUN_ID
            runtime_root = Path(directory) / "staging-runtime"
            compose = {"name": "binhu-staging"}
            with patch.object(migration, "BASE", Path(directory)), \
                 patch.object(migration, "_staging_runtime", return_value=(
                     runtime_root, {"environment": "staging"}, compose, "APP_ENVIRONMENT=staging\n")), \
                 patch.object(migration, "_candidate", return_value=(
                     Path(directory), candidate_manifest(), {"image_id": IMAGE_ID})), \
                 patch.object(migration, "_run_modules", return_value=MEASURED) as run_modules:
                result = migration.measure(RUN_ID, ARTIFACT_ID)
            self.assertEqual(result["candidate_run_id"], CANDIDATE_RUN)
            self.assertTrue((evidence / "measure.json").is_file())
            self.assertFalse((runtime_root / "measure.json").exists())
            run_modules.assert_called_once_with(
                runtime_root, compose, "APP_ENVIRONMENT=staging\n", IMAGE_ID,
                "measure", "0.32.2")

    def test_migration_uses_candidate_image_on_staging_network_with_fixed_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend.env").write_text("APP_ENVIRONMENT=staging\n", encoding="utf-8")
            network = {"networks": {"internal": {"name": "binhu-staging_internal"}}}
            output = json.dumps({"command": "measure", "consistent": False})
            with patch.object(migration, "_command", side_effect=[
                json.dumps([{"Labels": {
                    "binhu.environment": "staging",
                    "com.docker.compose.project": "binhu-staging",
                }}]), output,
            ]) as command:
                result = migration._migration_output(
                    root, network, "APP_ENVIRONMENT=staging\n", IMAGE_ID,
                    migration.MIGRATION_MODULES[0], "measure", "0.32.2")
            self.assertFalse(result["consistent"])
            docker_args = command.call_args_list[1].args[0]
            self.assertEqual(docker_args[-5:], [
                IMAGE_ID, "-B", "-m", migration.MIGRATION_MODULES[0], "measure",
            ])
            self.assertIn("binhu-staging_internal", docker_args)
            self.assertNotIn("init_db", docker_args)

    def test_migration_rejects_non_staging_network_before_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(migration, "_command") as command:
                with self.assertRaisesRegex(ValueError, "network_identity_mismatch"):
                    migration._migration_output(
                        Path(directory), {"networks": {"internal": {"name": "binhu-production_internal"}}},
                        "APP_ENVIRONMENT=staging\n", IMAGE_ID,
                        migration.MIGRATION_MODULES[0], "measure", "0.32.2")
            command.assert_not_called()

    def test_apply_requires_measure_and_rechecks_measurement_before_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_root = base / RUN_ID
            run_root.mkdir()
            (run_root / "measure.json").write_text(json.dumps({
                "artifact_id": ARTIFACT_ID, "state": "measured",
                "candidate_run_id": CANDIDATE_RUN, "image_id": IMAGE_ID,
                "measurement_hash": "stale",
            }), encoding="utf-8")
            with patch.object(migration, "BASE", base), \
                 patch.object(migration, "_candidate", return_value=(
                     base, candidate_manifest(), {"image_id": IMAGE_ID})), \
                 patch.object(migration, "_staging_runtime", return_value=(
                     base, {"environment": "staging"}, {"name": "binhu-staging"},
                     "APP_ENVIRONMENT=staging\n")), \
                 patch.object(migration, "_run_modules", return_value=MEASURED), \
                 patch.object(migration, "backup_databases") as backup:
                with self.assertRaisesRegex(ValueError, "schema_changed_after_measure"):
                    migration.apply(RUN_ID, ARTIFACT_ID)
            backup.assert_not_called()
            self.assertTrue((run_root / "failure.json").is_file())

    def test_apply_backups_and_runs_only_the_fixed_migrations(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_root = base / RUN_ID
            run_root.mkdir()
            (run_root / "measure.json").write_text(json.dumps({
                "artifact_id": ARTIFACT_ID, "state": "measured",
                "measurement_hash": migration._json_hash(MEASURED),
                "candidate_run_id": CANDIDATE_RUN, "image_id": IMAGE_ID, "commit": COMMIT,
                "version": "0.32.2",
            }), encoding="utf-8")
            migrated = {module: {"command": "migrate", "apply": True, "consistent": True}
                        for module in migration.MIGRATION_MODULES}
            with patch.object(migration, "BASE", base), \
                 patch.object(migration, "_candidate", return_value=(
                     base, candidate_manifest(), {"image_id": IMAGE_ID})), \
                 patch.object(migration, "_staging_runtime", return_value=(
                     base, {"environment": "staging"}, {"name": "binhu-staging"},
                     "APP_ENVIRONMENT=staging\n")), \
                 patch.object(migration, "_run_modules", side_effect=[MEASURED, migrated]) as run_modules, \
                 patch.object(migration, "backup_databases") as backup:
                result = migration.apply(RUN_ID, ARTIFACT_ID)
            backup.assert_called_once()
            self.assertEqual(run_modules.call_count, 2)
            self.assertTrue(result["database_backup"])
            self.assertEqual(result["state"], "applied")
            self.assertTrue((run_root / "apply.json").is_file())

    def test_verify_requires_all_modules_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_root = base / RUN_ID
            run_root.mkdir()
            (run_root / "apply.json").write_text(json.dumps({
                "artifact_id": ARTIFACT_ID, "state": "applied", "database_backup": True,
                "candidate_run_id": CANDIDATE_RUN, "image_id": IMAGE_ID,
                "commit": COMMIT, "version": "0.32.2",
                "measurement_hash": "hash",
            }), encoding="utf-8")
            verified = {module: {"command": "verify", "consistent": True}
                        for module in migration.MIGRATION_MODULES}
            with patch.object(migration, "BASE", base), \
                 patch.object(migration, "_candidate", return_value=(
                     base, candidate_manifest(), {"image_id": IMAGE_ID})), \
                 patch.object(migration, "_staging_runtime", return_value=(
                     base, {"environment": "staging"}, {"name": "binhu-staging"},
                     "APP_ENVIRONMENT=staging\n")), \
                 patch.object(migration, "_run_modules", return_value=verified):
                result = migration.verify(RUN_ID, ARTIFACT_ID)
            self.assertTrue(result["verified"])
            self.assertEqual(result["state"], "verified")


if __name__ == "__main__":
    unittest.main()
