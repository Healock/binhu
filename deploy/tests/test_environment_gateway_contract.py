import unittest
from pathlib import Path

from deploy.environments.runtime import SPEC
from deploy.environments import update

ROOT = Path(__file__).resolve().parents[2]


class EnvironmentGatewayContractTests(unittest.TestCase):
    def test_both_environment_update_actions_exist(self):
        source = (ROOT / "deploy/environments/update.py").read_text(encoding="utf-8")
        for action in ("measure-development", "apply-development", "measure-staging", "apply-staging"):
            self.assertIn(action, source)

    def test_environment_specs_are_isolated(self):
        self.assertEqual(SPEC["development"][:3], ("Dev_", "dev", 48125))
        self.assertEqual(SPEC["staging"][:3], ("Staging_", "staging", 48126))
        self.assertNotEqual(SPEC["development"][3], SPEC["staging"][3])

    def test_update_helpers_are_parameterized(self):
        self.assertTrue(hasattr(update, "measure_environment"))
        self.assertTrue(hasattr(update, "apply_environment"))


if __name__ == "__main__":
    unittest.main()
