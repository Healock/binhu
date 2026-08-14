import unittest
from datetime import date
from unittest.mock import patch

from services.star_rating_import import STAR_RATING_HEADERS
from services.visit_import import VISIT_HEADERS
from services.visit_source import VisitSourceError, fetch_rows, workbook_bytes


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("GET", "http://source.invalid/api")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("upstream error", request=request, response=response)


class FakeAsyncClient:
    post_responses = []
    get_responses = []
    instances = []

    def __init__(self, *, base_url, timeout, headers):
        self.base_url = base_url
        self.timeout = timeout
        self.headers = dict(headers)
        self.posts = []
        self.gets = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, path, *, params):
        self.posts.append((path, params.copy()))
        return type(self).post_responses.pop(0)

    async def get(self, path, *, params):
        self.gets.append((path, params.copy(), self.headers.copy()))
        return type(self).get_responses.pop(0)


def visit_row(police_name="吴江区公安局滨湖新城派出所"):
    return {
        "pcsname": police_name,
        "jgmc": "测试社区",
        "isPlate": "扫码",
        "dz": "测试地址",
        "trueName": "测试人员",
        "createBy": "test-account",
        "createTime": "2026-08-14 09:00:00",
        "checkRoomCnt": "2",
        "cnt1": "1",
        "cnt2": "1",
        "cnt3": "0",
    }


def rating_row():
    return {
        "pcsname": "滨湖新城派出所",
        "sssq": "测试社区",
        "address": "测试地址",
        "score": 90,
        "houselevelName": "三星出租房",
        "createtime": "2026-08-14 10:00:00",
        "yhxq": "",
    }


class VisitSourceAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAsyncClient.instances = []
        FakeAsyncClient.post_responses = []
        FakeAsyncClient.get_responses = []

    async def test_mock_rows_are_scoped_and_standardized(self):
        with patch("services.visit_source.settings.VISIT_SOURCE_MOCK", True):
            result = await fetch_rows("detail", date(2026, 8, 13), date(2026, 8, 13))
        self.assertEqual(result["valid_count"], 1)
        self.assertEqual(next(iter(result["rows"][0].values())), "滨湖新城派出所")
        self.assertTrue(workbook_bytes("detail", result["rows"]).startswith(b"PK"))

    async def test_unconfigured_source_does_not_call_network(self):
        with (
            patch("services.visit_source.settings.VISIT_SOURCE_MOCK", False),
            patch("services.visit_source.settings.VISIT_SOURCE_BASE_URL", ""),
        ):
            with self.assertRaises(VisitSourceError) as raised:
                await fetch_rows("rating", date(2026, 8, 13), date(2026, 8, 13))
        self.assertEqual(raised.exception.code, "not_configured")

    async def test_invalid_source_is_rejected(self):
        with self.assertRaises(VisitSourceError) as raised:
            await fetch_rows("unknown", date(2026, 8, 13), date(2026, 8, 13))
        self.assertEqual(raised.exception.code, "invalid_source")

    async def test_login_fetches_verified_visit_contract_and_canonicalizes_scope(self):
        FakeAsyncClient.post_responses = [FakeResponse({"code": 200, "data": "secret-token"})]
        FakeAsyncClient.get_responses = [FakeResponse({"code": 200, "data": [visit_row()]})]
        with (
            patch("services.visit_source.httpx.AsyncClient", FakeAsyncClient),
            patch("services.visit_source.settings.VISIT_SOURCE_MOCK", False),
            patch("services.visit_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.visit_source.settings.VISIT_SOURCE_AUTHORIZATION", ""),
            patch("services.visit_source.settings.VISIT_SOURCE_USERNAME", "reader"),
            patch("services.visit_source.settings.VISIT_SOURCE_PASSWORD", "private-password"),
        ):
            result = await fetch_rows("detail", date(2026, 8, 14), date(2026, 8, 14))

        client = FakeAsyncClient.instances[0]
        self.assertEqual(client.posts[0][0], "/api/login")
        self.assertEqual(client.gets[0][2]["Authorization"], "secret-token")
        self.assertEqual(client.gets[0][1]["pcsdm"], "320584710000")
        self.assertEqual(result["rows"][0][VISIT_HEADERS[0]], "滨湖新城派出所")
        self.assertEqual(result["rows"][0][VISIT_HEADERS[1]], "测试社区")
        self.assertEqual(result["rows"][0][VISIT_HEADERS[7]], "2")
        self.assertNotIn("private-password", repr(result))
        self.assertNotIn("secret-token", repr(result))

    async def test_static_authorization_skips_login_and_maps_rating_fields(self):
        FakeAsyncClient.get_responses = [FakeResponse({"code": "200", "data": [rating_row()]})]
        with (
            patch("services.visit_source.httpx.AsyncClient", FakeAsyncClient),
            patch("services.visit_source.settings.VISIT_SOURCE_MOCK", False),
            patch("services.visit_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.visit_source.settings.VISIT_SOURCE_AUTHORIZATION", "static-token"),
            patch("services.visit_source.settings.VISIT_SOURCE_USERNAME", ""),
            patch("services.visit_source.settings.VISIT_SOURCE_PASSWORD", ""),
        ):
            result = await fetch_rows("rating", date(2026, 8, 1), date(2026, 8, 14))

        client = FakeAsyncClient.instances[0]
        self.assertEqual(client.posts, [])
        self.assertNotIn("pcsdm", client.gets[0][1])
        self.assertEqual(result["rows"][0][STAR_RATING_HEADERS[1]], "测试社区")
        self.assertEqual(result["rows"][0][STAR_RATING_HEADERS[4]], "三星出租房")

    async def test_business_error_is_not_interpreted_as_empty_data(self):
        FakeAsyncClient.get_responses = [FakeResponse({"code": 500, "message": "internal detail"})]
        with (
            patch("services.visit_source.httpx.AsyncClient", FakeAsyncClient),
            patch("services.visit_source.settings.VISIT_SOURCE_MOCK", False),
            patch("services.visit_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.visit_source.settings.VISIT_SOURCE_AUTHORIZATION", "static-token"),
        ):
            with self.assertRaises(VisitSourceError) as raised:
                await fetch_rows("detail", date(2026, 8, 14), date(2026, 8, 14))
        self.assertEqual(raised.exception.code, "upstream_error")
        self.assertNotIn("internal detail", raised.exception.message)

    async def test_credentials_are_required_without_static_authorization(self):
        with (
            patch("services.visit_source.settings.VISIT_SOURCE_MOCK", False),
            patch("services.visit_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.visit_source.settings.VISIT_SOURCE_AUTHORIZATION", ""),
            patch("services.visit_source.settings.VISIT_SOURCE_USERNAME", ""),
            patch("services.visit_source.settings.VISIT_SOURCE_PASSWORD", ""),
        ):
            with self.assertRaises(VisitSourceError) as raised:
                await fetch_rows("detail", date(2026, 8, 14), date(2026, 8, 14))
        self.assertEqual(raised.exception.code, "authentication_required")

    async def test_login_business_error_is_reported_as_authentication_failure(self):
        FakeAsyncClient.post_responses = [
            FakeResponse({"code": 401, "message": "credential detail"})
        ]
        with (
            patch("services.visit_source.httpx.AsyncClient", FakeAsyncClient),
            patch("services.visit_source.settings.VISIT_SOURCE_MOCK", False),
            patch("services.visit_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.visit_source.settings.VISIT_SOURCE_AUTHORIZATION", ""),
            patch("services.visit_source.settings.VISIT_SOURCE_USERNAME", "reader"),
            patch("services.visit_source.settings.VISIT_SOURCE_PASSWORD", "private-password"),
        ):
            with self.assertRaises(VisitSourceError) as raised:
                await fetch_rows("detail", date(2026, 8, 14), date(2026, 8, 14))
        self.assertEqual(raised.exception.code, "authentication_failed")
        self.assertNotIn("credential detail", raised.exception.message)

    async def test_unrelated_station_is_rejected(self):
        FakeAsyncClient.get_responses = [
            FakeResponse({"code": 200, "data": [visit_row("其他派出所")]})
        ]
        with (
            patch("services.visit_source.httpx.AsyncClient", FakeAsyncClient),
            patch("services.visit_source.settings.VISIT_SOURCE_MOCK", False),
            patch("services.visit_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.visit_source.settings.VISIT_SOURCE_AUTHORIZATION", "static-token"),
        ):
            with self.assertRaises(VisitSourceError) as raised:
                await fetch_rows("detail", date(2026, 8, 14), date(2026, 8, 14))
        self.assertEqual(raised.exception.code, "scope_or_schema")

    async def test_repeated_full_page_is_stopped(self):
        full_page = [visit_row() for _ in range(200)]
        FakeAsyncClient.get_responses = [
            FakeResponse({"code": 200, "data": full_page}),
            FakeResponse({"code": 200, "data": full_page}),
        ]
        with (
            patch("services.visit_source.httpx.AsyncClient", FakeAsyncClient),
            patch("services.visit_source.settings.VISIT_SOURCE_MOCK", False),
            patch("services.visit_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.visit_source.settings.VISIT_SOURCE_AUTHORIZATION", "static-token"),
        ):
            with self.assertRaises(VisitSourceError) as raised:
                await fetch_rows("detail", date(2026, 8, 14), date(2026, 8, 14))
        self.assertEqual(raised.exception.code, "pagination_repeated")


if __name__ == "__main__":
    unittest.main()
