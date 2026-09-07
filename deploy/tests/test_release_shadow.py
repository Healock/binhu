import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "release-shadow" / "prepare.py"
SPEC = importlib.util.spec_from_file_location("release_shadow_prepare", MODULE_PATH)
prepare = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(prepare)


class ReleaseShadowPrepareTests(unittest.TestCase):
    def test_database_names_are_exact_eight_and_fit_mysql_identifier_limit(self):
        names = prepare.build_database_names("20260907-a1")
        self.assertEqual(list(names), list(prepare.DOMAINS))
        self.assertEqual(len(set(names.values())), 8)
        self.assertTrue(all(len(name) <= 64 for name in names.values()))
        self.assertEqual(names["WorkflowData"], "ReleaseShadow_20260907_a1_7")

    def test_long_run_id_is_rejected_before_database_generation(self):
        with self.assertRaises(ValueError):
            prepare.validate_run_id("x" * 33)

    def test_bind_mounts_must_resolve_inside_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "backend").mkdir()
            (root / "artifacts").mkdir()
            (root / "validation").mkdir()
            prepare.validate_bind_mounts(root, root / "backend", root / "artifacts", root / "validation")
            outside = root.parent / (root.name + "-outside")
            outside.mkdir()
            try:
                (root / "backend-link").symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ValueError):
                    prepare.validate_bind_mounts(root, root / "backend-link")
            finally:
                outside.rmdir()

    def test_prepare_source_carries_validation_runner_and_pythonpath_contract(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("validation/verify.py", source)
        self.assertIn("validation/summary_verify.py", source)
        self.assertIn("PYTHONPATH", source)
        self.assertIn("docker", source)

    def test_compose_declares_named_volume_without_double_project_prefix(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("'data:/var/lib/mysql'", source)
        self.assertIn("'name': volume_name", source)

    def test_verify_records_guard_failures_and_uses_explicit_checks(self):
        source = (ROOT / "deploy" / "release-shadow" / "verify.py").read_text(encoding="utf-8")
        self.assertNotIn("assert ", source)
        self.assertIn("expected_databases", source)
        self.assertIn("mysql-bootstrap.json", source)
        self.assertIn("error_type", source)
        self.assertIn("TXDOCS_ENABLED", source)


if __name__ == "__main__":
    unittest.main()
