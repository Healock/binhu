import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class MacMockContractTest(unittest.TestCase):
    def _module(self, directory):
        import importlib
        with patch.dict(os.environ, {"MACMOCK_WRITE_TOKEN": "test-token", "MACMOCK_STATE_FILE": str(Path(directory) / "mac")}, clear=False):
            module = importlib.import_module("app")
            return importlib.reload(module)

    def test_normalize_and_protected_write(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self._module(directory)
            client = module.app.test_client()
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.post("/", json={"mac": "aa-bb-cc-dd-ee-ff"}).status_code, 403)
            response = client.post("/", json={"mac": "aa-bb-cc-dd-ee-ff"}, headers={"X-Macmock-Token": "test-token"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/").get_json()["mac"], "AA:BB:CC:DD:EE:FF")
            preflight = client.options("/")
            self.assertEqual(preflight.status_code, 204)
            self.assertIn("X-Macmock-Token", preflight.headers["Access-Control-Allow-Headers"])
            self.assertEqual(client.get("/health").get_json(), {"status": "ok"})

    def test_invalid_mac_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self._module(directory)
            response = module.app.test_client().post("/", json={"mac": "not-a-mac"}, headers={"X-Macmock-Token": "test-token"})
            self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
