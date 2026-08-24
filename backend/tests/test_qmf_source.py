import os
import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.qmf_source import (
    MODEL_THREE_ENDPOINT,
    QmfSourceError,
    fetch_pending_rows,
    normalize_source_row,
    resolve_rows,
)


VALID_IDENTITY = "11010519491231002X"


def _settings_patch():
    return patch.multiple(
        "config.settings",
        QMF_SOURCE_BASE_URL="http://legacy.invalid",
        QMF_SOURCE_TIMEOUT_SECONDS=5,
        QMF_SOURCE_MAX_PAGES=10,
        QMF_SOURCE_MAX_RECORDS=1000,
        VISIT_SOURCE_AUTHORIZATION="",
        VISIT_SOURCE_USERNAME="fictional-user",
        VISIT_SOURCE_PASSWORD="fictional-password",
        VISIT_SOURCE_LOGIN_PATH="/api/login",
        QMF_EXPECTED_STATION_CODE="320584710000",
        QMF_EXPECTED_STATION_NAME="滨湖新城派出所",
    )


class OrganizationCursor:
    def __init__(self):
        self.rows = []

    async def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        if "FROM _communities AS c LEFT JOIN _community_aliases AS a" in normalized:
            self.rows = [(1, "长板社区", "芦荡社区"), (2, "水秀社区", None)]
        elif "FROM _qmf_organization_codes" in normalized:
            code = (params or ("",))[0]
            self.rows = [(1, "长板社区")] if code == "320584710103" else []
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    async def fetchall(self):
        return list(self.rows)


class QmfSourceTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_source_row_maps_observed_legacy_fields(self):
        row = normalize_source_row({
            "endTime": "2026-08-24 18:00:00",
            "hcczr": "网格员甲",
            "xm": "测试人员",
            "sfzh": " 11010519491231002x ",
            "lxfs": "13800000000",
            "dz": "测试路1号",
            "jgmc": "芦荡社区",
        })
        self.assertEqual(row["身份证号"], VALID_IDENTITY)
        self.assertEqual(row["下发社区"], "芦荡社区")
        self.assertEqual(row["核查结果"], "")

    async def test_fetch_uses_read_only_pending_query_and_filters_rows(self):
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/login":
                self.assertEqual(request.method, "POST")
                params = parse_qs(request.url.query.decode())
                self.assertEqual(params["username"], ["fictional-user"])
                return httpx.Response(200, json={"code": 200, "data": "fictional-token"})
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, MODEL_THREE_ENDPOINT)
            params = parse_qs(request.url.query.decode())
            self.assertEqual(params["judgeType"], ["yys"])
            self.assertEqual(params["hcjg"], ["0"])
            self.assertEqual(params["pcsbm"], ["320584710000"])
            self.assertEqual(params["pageSize"], ["200"])
            return httpx.Response(200, json={
                "code": 200,
                "data": {"total": 4, "list": [
                    {
                        "pcsname": "滨湖新城派出所", "hcjg": "0", "sfzh": VALID_IDENTITY,
                        "xm": "测试人员", "jgmc": "芦荡社区", "xfsq": "320584710103",
                    },
                    {
                        "pcsname": "其他派出所", "hcjg": "0", "sfzh": "120000000000000000",
                    },
                    {
                        "pcsname": "滨湖新城派出所", "hcjg": "1", "sfzh": "120000000000000001",
                    },
                    {
                        "pcsname": "滨湖新城派出所", "hcjg": "0", "sfzh": "",
                    },
                ]},
            })

        with _settings_patch():
            result = await fetch_pending_rows(transport=httpx.MockTransport(handler))

        self.assertEqual(result["record_count"], 4)
        self.assertEqual(result["valid_count"], 1)
        self.assertEqual(result["issue_count"], 2)
        self.assertEqual(result["rows"][0]["__organization_code"], "320584710103")
        self.assertEqual(len(requests), 2)
        self.assertFalse(any(
            request.url.path != "/api/login" and request.method != "GET"
            for request in requests
        ))

    async def test_resolve_rows_uses_organization_code_and_keeps_unresolved_rows_out(self):
        result = {
            "rows": [
                {
                    "姓名": "可匹配", "身份证号": VALID_IDENTITY, "下发社区": "来源名",
                    "__organization_code": "320584710103",
                },
                {
                    "姓名": "不可匹配", "身份证号": "120000000000000001", "下发社区": "未知",
                    "__organization_code": "UNKNOWN",
                },
            ],
            "record_count": 2,
            "valid_count": 2,
            "issue_count": 0,
            "issues": [],
        }
        resolved = await resolve_rows(OrganizationCursor(), result)
        self.assertEqual(len(resolved["rows"]), 1)
        self.assertEqual(resolved["rows"][0]["下发社区"], "长板社区")
        self.assertEqual(resolved["unresolved_count"], 1)
        self.assertEqual(resolved["unresolved"][0]["reason"], "组织编码和来源社区名均无法匹配")

    async def test_invalid_source_business_response_is_rejected(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/login":
                return httpx.Response(200, json={"code": 200, "data": "fictional-token"})
            return httpx.Response(200, json={"code": 500, "msg": "failure"})

        with _settings_patch():
            with self.assertRaisesRegex(QmfSourceError, "模型三来源返回业务错误"):
                await fetch_pending_rows(transport=httpx.MockTransport(handler))


if __name__ == "__main__":
    unittest.main()
