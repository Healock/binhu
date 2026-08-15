from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_certificate_source import CERTIFICATE_ENDPOINT, fetch_certificate_rows


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "code": 200,
            "data": {
                "records": [
                    {"pcsname": "滨湖新城派出所", "sssq": "长板社区", "dz": "测试路1号", "czrxm": "甲"},
                    {"pcsname": "其他派出所", "sssq": "其他社区", "dz": "测试路2号", "czrxm": "乙"},
                ]
            },
        }


class _Client:
    last_request = None

    def __init__(self, *args, headers=None, **kwargs):
        self.headers = dict(headers or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, endpoint, params=None):
        type(self).last_request = (endpoint, params)
        return _Response()


class RegistryCertificateSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_all_dates_and_rejects_other_police_stations(self):
        with (
            patch("services.registry_certificate_source.httpx.AsyncClient", _Client),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_AUTHORIZATION", "token"),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_POLICE_CODE", "320584710000"),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_POLICE_NAME", "滨湖新城派出所"),
        ):
            result = await fetch_certificate_rows()
        self.assertEqual(result["valid_count"], 1)
        self.assertEqual(result["issue_count"], 1)
        self.assertEqual(result["rows"][0]["source_row"], 1)
        endpoint, params = _Client.last_request
        self.assertEqual(endpoint, CERTIFICATE_ENDPOINT)
        self.assertNotIn("startTime", params)
        self.assertNotIn("endTime", params)
        self.assertEqual(params["deptCode"], "320584710000")


if __name__ == "__main__":
    unittest.main()
