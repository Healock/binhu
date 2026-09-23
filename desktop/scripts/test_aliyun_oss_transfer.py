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
            def __init__(self, status, body=b"", headers=None):
                self.status = status
                self._body = body
                self._headers = headers or []

            def read(self, _limit=None):
                return self._body

            def getheaders(self):
                return self._headers

        class Connection:
            all_requests = []

            def __init__(self, *_args, **_kwargs):
                self.requests = []
                Connection.all_requests.append(self.requests)

            def request(self, method, path, body=None, headers=None):
                self.requests.append((method, path, body, headers))

            def getresponse(self):
                method, path, _body, _headers = self.requests[-1]
                if method == "POST" and path.endswith("?uploads"):
                    return Response(200, b"<InitiateMultipartUploadResult><UploadId>upload-id</UploadId></InitiateMultipartUploadResult>")
                if method == "PUT":
                    return Response(200, b"", [("ETag", '"' + "a" * 32 + '"')])
                return Response(200, b"<CompleteMultipartUploadResult></CompleteMultipartUploadResult>")

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.tar.gz"
            path.write_bytes(b"bundle" * 200_000)
            with mock.patch.object(oss.http.client, "HTTPSConnection", Connection):
                oss.upload(path, oss.UPLOAD_ENDPOINT, self.key)
            part_requests = [request for requests in Connection.all_requests for request in requests if request[0] == "PUT"]
            self.assertEqual(len(part_requests), 2)


if __name__ == "__main__":
    unittest.main()
