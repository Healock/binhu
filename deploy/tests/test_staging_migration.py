import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.environments import staging_migration as migration


class StagingMigrationTests(unittest.TestCase):
    def test_contract_is_staging_only_and_fixed(self):
        source = (Path(__file__).parents[1] / "environments" / "staging_migration.py").read_text(encoding="utf-8")
        wrapper = (Path(__file__).parents[1] / "environments" / "binhu-staging-application-gateway").read_text(encoding="utf-8")
        self.assertIn('APP_ENVIRONMENT != "staging"', source)
        self.assertIn('not name.startswith(PREFIX)', source)
        self.assertIn('staging-migrate-[0-9a-f]{16}', wrapper)
        self.assertIn('^(measure|apply|verify)$', wrapper)
        self.assertIn('production_modified', source)

    def test_apply_requires_measurement_and_keeps_existing_evidence(self):
        run_id = "staging-migrate-" + "a" * 16
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / run_id
            root.mkdir(parents=True)
            (root / "measure.json").write_text(json.dumps({
                "artifact_id": "b" * 64, "state": "measured"
            }), encoding="utf-8")
            with patch.object(migration, "BASE", Path(directory)), \
                 patch.object(migration, "_staging_identity", return_value=(Path(directory),
                     {"artifact_id": "b" * 64, "commit": "c" * 40, "version": "0.30.24", "hashes": {}},
                     {"name": "binhu-staging"}, "APP_ENVIRONMENT=staging\n")), \
                 patch.object(migration, "verify_database_identity"), \
                 patch.object(migration, "_backend_container", return_value="d" * 64), \
                 patch.object(migration, "backup_databases"), \
                 patch.object(migration, "_run_inner", return_value={
                     "before": {"schema_hash": "1"}, "after": {"schema_hash": "2"}
                 }):
                result = migration.apply(run_id, "b" * 64)
            self.assertEqual(result["state"], "applied")
            self.assertTrue(result["database_backup"])
            self.assertTrue((root / "apply.json").is_file())
            with self.assertRaisesRegex(ValueError, "measure_required"):
                migration.apply("staging-migrate-" + "e" * 16, "b" * 64)

    def test_verify_rejects_schema_change_after_apply(self):
        run_id = "staging-migrate-" + "a" * 16
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / run_id
            root.mkdir(parents=True)
            (root / "apply.json").write_text(json.dumps({
                "artifact_id": "b" * 64, "state": "applied", "schema_hash": "expected",
                "before": {"schema_hash": "before"}, "commit": "c" * 40,
                "version": "0.30.24", "database_backup": True
            }), encoding="utf-8")
            with patch.object(migration, "BASE", Path(directory)), \
                 patch.object(migration, "_staging_identity", return_value=(Path(directory),
                     {"artifact_id": "b" * 64}, {"name": "binhu-staging"}, "APP_ENVIRONMENT=staging\n")), \
                 patch.object(migration, "_backend_container", return_value="d" * 64), \
                 patch.object(migration, "_run_inner", return_value={
                     "before": {"schema_hash": "now"}, "after": {"schema_hash": "changed"}
                 }):
                with self.assertRaisesRegex(ValueError, "schema_changed"):
                    migration.verify(run_id, "b" * 64)
            failure = json.loads((root / "failure.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["reason"], "staging_migration_schema_changed_after_apply")


if __name__ == "__main__":
    unittest.main()
