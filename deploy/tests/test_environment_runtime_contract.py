import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]

class EnvironmentRuntimeContractTests(unittest.TestCase):
    def test_nonproduction_compose_publishes_only_loopback_and_not_internal_network(self):
        source = (ROOT / "deploy/environments/runtime.py").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:{port}:37125", source)
        self.assertIn("'internal': False", source)
        self.assertNotIn("'internal': True", source)

    def test_runtime_script_compiles(self):
        ast.parse((ROOT / "deploy/environments/runtime.py").read_text(encoding="utf-8"))
