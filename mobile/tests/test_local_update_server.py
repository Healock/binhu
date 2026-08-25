import http.client
import tempfile
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from mobile.scripts.local_update_server import UpdateRequestHandler


class LocalUpdateServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "manifest.stable.json").write_text('{"version":"0.25.28"}', encoding="utf-8")
        (self.root / "Binhu-Android-arm64-0.25.28.apk").write_bytes(b"0123456789")
        handler = partial(UpdateRequestHandler, directory=str(self.root))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, path: str, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        result = response.status, dict(response.getheaders()), body
        connection.close()
        return result

    def test_manifest_uses_no_store(self):
        status, headers, body = self.request("/manifest.stable.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"0.25.28", body)

    def test_apk_supports_resume_ranges(self):
        status, headers, body = self.request(
            "/Binhu-Android-arm64-0.25.28.apk",
            {"Range": "bytes=4-7"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 4-7/10")
        self.assertEqual(body, b"4567")

    def test_invalid_range_is_rejected(self):
        status, _, _ = self.request(
            "/Binhu-Android-arm64-0.25.28.apk",
            {"Range": "bytes=99-"},
        )
        self.assertEqual(status, 416)


if __name__ == "__main__":
    unittest.main()
