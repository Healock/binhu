import tempfile
import unittest
from pathlib import Path

from fixture import make_tasks, make_users, write_manifest
from shadow_guard import ShadowSafetyError, validate_shadow_environment


class ShadowToolTests(unittest.TestCase):
    def test_fixture_counts_match_plan(self):
        self.assertEqual(len(make_users()), 76)
        self.assertEqual(len(make_tasks()), 3600)

    def test_guard_rejects_production_and_mismatched_project(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            marker.write_text("shadow:LT-20260902-01", encoding="utf-8")
            env = {
                "APP_ENVIRONMENT": "production",
                "LOAD_TEST_RUN_ID": "LT-20260902-01",
                "COMPOSE_PROJECT_NAME": "binhu-loadtest-lt-20260902-01",
                "SHADOW_DB_HOST": "mysql-shadow",
                "SHADOW_DB_NAME": "LoadTest_LT_20260902_01",
                "SHADOW_MARKER_FILE": str(marker),
            }
            with self.assertRaises(ShadowSafetyError):
                validate_shadow_environment("LT-20260902-01", env)
            env["APP_ENVIRONMENT"] = "shadow"
            env["COMPOSE_PROJECT_NAME"] = "binhu"
            with self.assertRaises(ShadowSafetyError):
                validate_shadow_environment("LT-20260902-01", env)

    def test_guard_accepts_exact_shadow_target(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            marker.write_text("shadow:LT-20260902-01", encoding="utf-8")
            env = {
                "APP_ENVIRONMENT": "shadow",
                "LOAD_TEST_RUN_ID": "LT-20260902-01",
                "COMPOSE_PROJECT_NAME": "binhu-loadtest-lt-20260902-01",
                "SHADOW_DB_HOST": "mysql-shadow",
                "SHADOW_DB_NAME": "LoadTest_LT_20260902_01",
                "SHADOW_MARKER_FILE": str(marker),
            }
            context = validate_shadow_environment("LT-20260902-01", env)
            self.assertEqual(context.project, "binhu-loadtest-lt-20260902-01")


if __name__ == "__main__":
    unittest.main()
