import hashlib
import hmac
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop.scripts import aliyun_oss_transfer as oss


class AliyunOssTransferTests(unittest.TestCase):
    def setUp(self):
        self.key = "client-transfer/0.30.21/" + "a" * 40 + "/binhu-clients-0.30.21.tar.gz"
        self.credentials = mock.patch.dict(
            os.environ,
            {"ALIYUN_OSS_ACCESS_KEY_ID": "test-id", "ALIYUN_OSS_ACCESS_KEY_SECRET": "test-secret"},
            clear=False,
        )
        self.credentials.start()

    def tearDown(self):
        self.credentials.stop()

    def test_presign_uses_fixed_internal_endpoint_and_canonical_resource(self):
        url = oss.presigned_url(oss.INTERNAL_ENDPOINT, self.key, 3600)
        self.assertTrue(url.startswith("https://" + oss.INTERNAL_ENDPOINT + "/" + self.key + "?"))
        self.assertIn("OSSAccessKeyId=test-id", url)
        self.assertIn("Expires=", url)
        self.assertIn("Signature=", url)

    def test_presign_rejects_wrong_endpoint_and_key(self):
        with self.assertRaises(SystemExit):
            oss.presigned_url(oss.PUBLIC_ENDPOINT, self.key, 3600)
        with self.assertRaises(SystemExit):
            oss.presigned_url(oss.INTERNAL_ENDPOINT, "other/file.tar.gz", 3600)

    def test_upload_streams_bundle_without_printing_secret(self):
        class Response:
            status = 200

            def read(self):
                return b""

        class Connection:
            def __init__(self, *_args, **_kwargs):
                self.sent = b""

            def putrequest(self, *_args, **_kwargs):
                pass

            def putheader(self, *_args, **_kwargs):
                pass

            def endheaders(self):
                pass

            def send(self, chunk):
                self.sent += chunk

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.tar.gz"
            path.write_bytes(b"bundle")
            with mock.patch.object(oss.http.client, "HTTPSConnection", Connection):
                oss.upload(path, oss.UPLOAD_ENDPOINT, self.key)


if __name__ == "__main__":
    unittest.main()
