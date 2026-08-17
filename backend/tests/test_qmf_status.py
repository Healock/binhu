import os
import unittest
from urllib.parse import parse_qs

import httpx

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.qmf_status import (
    QmfLegacyStatusClient,
    QmfStatusConfig,
    STATUS_AMBIGUOUS,
    STATUS_COMPLETED_MATCH,
    STATUS_COMPLETED_MISMATCH,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    STATUS_STATION_MISMATCH,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN_RESULT,
    normalize_legacy_result,
)


VALID_IDENTITY = "11010519491231002X"


def status_config() -> QmfStatusConfig:
    return QmfStatusConfig(
        base_url="http://legacy.invalid",
        authorization="",
        username="fictional-user",
        password="fictional-password",
        login_path="/api/login",
        timeout_seconds=5,
        expected_station_name="滨湖新城派出所",
    )


def response_row(*, code="2", text="在吴", station="滨湖新城派出所"):
    return {
        "sfzh": VALID_IDENTITY,
        "pcsname": station,
        "hcjg": code,
        "hcjgtext": text,
        "hcsj": "2026-08-16 07:00:00",
    }


class QmfLegacyStatusTests(unittest.IsolatedAsyncioTestCase):
    def test_result_normalization_covers_three_results_and_pending(self):
        self.assertEqual(normalize_legacy_result("0", "未核查"), (STATUS_PENDING, ""))
        self.assertEqual(normalize_legacy_result("1", "离开不返吴(注销)"), ("completed", "离开不返吴"))
        self.assertEqual(normalize_legacy_result("2", "在吴"), ("completed", "在吴"))
        self.assertEqual(normalize_legacy_result("3", "近期反吴"), ("completed", "近期返吴"))
        self.assertEqual(normalize_legacy_result("3", "近期返吴(不注销)"), ("completed", "近期返吴"))
        self.assertEqual(normalize_legacy_result("9", "其他"), (STATUS_UNKNOWN_RESULT, ""))

    def test_management_code_and_display_text_are_cross_checked(self):
        expected_by_code = {
            "0": (STATUS_PENDING, ""),
            "1": ("completed", "离开不返吴"),
            "2": ("completed", "在吴"),
            "3": ("completed", "近期返吴"),
        }
        for code, expected in expected_by_code.items():
            with self.subTest(code=code):
                self.assertEqual(normalize_legacy_result(code, ""), expected)
        for code, text in (
            ("1", "近期返吴"),
            ("1", "近期返吴(不注销)"),
            ("3", "离开不返吴"),
            ("2", "未核查"),
        ):
            with self.subTest(code=code, text=text):
                self.assertEqual(
                    normalize_legacy_result(code, text),
                    (STATUS_UNKNOWN_RESULT, ""),
                )

    async def test_exact_query_logs_in_and_returns_completed_match(self):
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/login":
                self.assertEqual(request.method, "POST")
                params = parse_qs(request.url.query.decode())
                self.assertEqual(params["username"], ["fictional-user"])
                return httpx.Response(200, json={"code": 200, "data": "fictional-token"})
            self.assertEqual(request.url.path, "/api/masses/queryYysList")
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["Authorization"], "fictional-token")
            params = parse_qs(request.url.query.decode())
            self.assertEqual(params["judgeType"], ["yys"])
            self.assertEqual(params["sfzh"], [VALID_IDENTITY])
            return httpx.Response(200, json={
                "code": 200,
                "data": {"total": 1, "list": [response_row()]},
            })

        result = await QmfLegacyStatusClient(
            config=status_config(),
            transport=httpx.MockTransport(handler),
        ).query(identity=VALID_IDENTITY, expected_result="在吴")
        self.assertEqual(result.state, STATUS_COMPLETED_MATCH)
        self.assertTrue(result.matches_platform_result)
        self.assertEqual(result.result, "在吴")
        self.assertEqual(result.checked_at, "2026-08-16 07:00:00")
        self.assertEqual(len(requests), 2)
        self.assertFalse(any(
            token in request.url.path
            for request in requests
            for token in ("uploadPhoto", "saveLocalPhoto", "addPeople", "fnmxCheck")
        ))

    async def test_completed_mismatch_pending_and_not_found(self):
        async def query(row, total=1):
            async def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={
                    "code": 200,
                    "data": {"total": total, "list": [] if row is None else [row]},
                })

            config = QmfStatusConfig(**{
                **status_config().__dict__,
                "authorization": "fixture-token",
            })
            return await QmfLegacyStatusClient(
                config=config,
                transport=httpx.MockTransport(handler),
            ).query(identity=VALID_IDENTITY, expected_result="近期返吴")

        mismatch = await query(response_row(code="2", text="在吴"))
        self.assertEqual(mismatch.state, STATUS_COMPLETED_MISMATCH)
        self.assertFalse(mismatch.matches_platform_result)

        matching_recent_return = await query(response_row(code="3", text="近期返吴(不注销)"))
        self.assertEqual(matching_recent_return.state, STATUS_COMPLETED_MATCH)
        self.assertTrue(matching_recent_return.matches_platform_result)
        self.assertEqual(matching_recent_return.result, "近期返吴")

        pending = await query(response_row(code="0", text="未核查"))
        self.assertEqual(pending.state, STATUS_PENDING)
        self.assertIsNone(pending.matches_platform_result)

        missing = await query(None, total=0)
        self.assertEqual(missing.state, STATUS_NOT_FOUND)

    async def test_ambiguous_station_unknown_and_invalid_shape_stop_safely(self):
        async def query(payload):
            async def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={"code": 200, "data": payload})

            config = QmfStatusConfig(**{
                **status_config().__dict__,
                "authorization": "fixture-token",
            })
            return await QmfLegacyStatusClient(
                config=config,
                transport=httpx.MockTransport(handler),
            ).query(identity=VALID_IDENTITY, expected_result="在吴")

        ambiguous = await query({
            "total": 2,
            "list": [response_row(), response_row()],
        })
        self.assertEqual(ambiguous.state, STATUS_AMBIGUOUS)

        station = await query({
            "total": 1,
            "list": [response_row(station="虚构其他派出所")],
        })
        self.assertEqual(station.state, STATUS_STATION_MISMATCH)

        unknown = await query({
            "total": 1,
            "list": [response_row(code="9", text="虚构新结果")],
        })
        self.assertEqual(unknown.state, STATUS_UNKNOWN_RESULT)
        self.assertNotIn("虚构新结果", unknown.reason)

        unavailable = await query({"unexpected": []})
        self.assertEqual(unavailable.state, STATUS_UNAVAILABLE)

    async def test_retry_policy_retries_gateway_error_once_but_not_http_500(self):
        attempts = 0

        async def retry_handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={
                "code": 200,
                "data": {"total": 0, "list": []},
            })

        config = QmfStatusConfig(**{
            **status_config().__dict__,
            "authorization": "fixture-token",
        })
        result = await QmfLegacyStatusClient(
            config=config,
            transport=httpx.MockTransport(retry_handler),
        ).query(identity=VALID_IDENTITY, expected_result="在吴")
        self.assertEqual(result.state, STATUS_NOT_FOUND)
        self.assertEqual(attempts, 2)

        attempts = 0

        async def no_retry_handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(500, text="sensitive upstream detail")

        result = await QmfLegacyStatusClient(
            config=config,
            transport=httpx.MockTransport(no_retry_handler),
        ).query(identity=VALID_IDENTITY, expected_result="在吴")
        self.assertEqual(result.state, STATUS_UNAVAILABLE)
        self.assertEqual(attempts, 1)
        self.assertNotIn("sensitive", result.reason)


if __name__ == "__main__":
    unittest.main()
