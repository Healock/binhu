import json
import os
import unittest
from pathlib import Path

import httpx

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.residence_platform import (  # noqa: E402
    ResidencePlatformClient,
    ResidencePlatformError,
    classify_floating_response,
)
from services.residence_platform_config import (  # noqa: E402
    ResidencePlatformConfig,
    residence_username,
    serialize_residence_value,
)
from services.qmf_config import decrypt_secret  # noqa: E402


VALID_IDENTITY = "11010519491231002X"


def config(**overrides) -> ResidencePlatformConfig:
    values = {
        "enabled": True,
        "base_url": "https://residence.invalid/grandlynn-boot",
        "username": "fixture-user",
        "password": "fixture-password",
        "mac_service_url": "http://mac.invalid",
        "access_token": "fixture-token",
        "organization_code": "3205840377",
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return ResidencePlatformConfig(**values)


class ResidencePlatformTests(unittest.IsolatedAsyncioTestCase):
    def test_community_account_is_derived_from_qmf_code(self):
        self.assertEqual(residence_username("A123456789"), "A12345678900")
        with self.assertRaises(ValueError):
            residence_username("320584")

    def test_residence_status_schema_tracks_safe_total_duration(self):
        source = Path(__file__).parents[1].joinpath("services", "residence_status_scan.py").read_text(encoding="utf-8")
        self.assertIn("duration_ms INT UNSIGNED DEFAULT NULL", source)
        self.assertIn("time.perf_counter()", source)

    def test_confirmed_no_data_contract_is_the_only_first_registration_result(self):
        self.assertEqual(
            classify_floating_response({
                "success": False,
                "code": 500,
                "message": "操作失败，没有查询到数据",
                "result": None,
            }).state,
            "first_registration",
        )
        for payload in (
            {"success": False, "code": 500, "message": "操作失败", "result": None},
            {"success": True, "code": 500, "message": "没有查询到数据", "result": None},
            {"success": False, "code": 500, "message": "没有查询到数据", "result": {}},
            {"success": False, "code": "unexpected", "message": "没有查询到数据", "result": None},
            None,
        ):
            with self.subTest(payload=payload):
                result = classify_floating_response(payload)
                self.assertNotEqual(result.state, "first_registration")

    def test_registered_contract_requires_a_result_object(self):
        registered = classify_floating_response({
            "success": True,
            "code": 200,
            "message": "查询成功",
            "result": {"fixture": "person"},
        })
        self.assertEqual(registered.state, "registered")
        self.assertEqual(registered.error_code, "")

        invalid = classify_floating_response({
            "success": True,
            "code": 200,
            "message": "查询成功",
            "result": None,
        })
        self.assertEqual((invalid.state, invalid.error_code), ("error", "business_error"))

    def test_secret_values_are_encrypted_before_storage(self):
        for key in ("residence_username", "residence_password", "residence_access_token"):
            with self.subTest(key=key):
                stored = serialize_residence_value(key, "fixture-secret")
                self.assertNotIn("fixture-secret", stored)
                self.assertEqual(decrypt_secret(stored), "fixture-secret")

    async def test_lookup_replays_the_confirmed_two_read_only_requests(self):
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            self.assertEqual(request.headers.get("X-Access-Token"), "fixture-token")
            self.assertEqual(request.headers.get("tenant_id"), "0")
            body = json.loads(request.content.decode("utf-8"))
            self.assertEqual(body, {"sfzh": VALID_IDENTITY, "xzqh": "320584"})
            if request.url.path.endswith("/szjzz/searchIsck"):
                return httpx.Response(200, json={
                    "success": True,
                    "code": 200,
                    "message": "没有查询到数据",
                    "result": None,
                })
            if request.url.path.endswith("/szjzz/searchzzrk"):
                return httpx.Response(200, json={
                    "success": False,
                    "code": 500,
                    "message": "操作失败，没有查询到数据",
                    "result": None,
                })
            return httpx.Response(404)

        result = await ResidencePlatformClient(
            config(),
            transport=httpx.MockTransport(handler),
        ).lookup(VALID_IDENTITY)

        self.assertEqual(result.state, "first_registration")
        self.assertEqual(
            [request.url.path for request in requests],
            [
                "/grandlynn-boot/szjzz/searchIsck",
                "/grandlynn-boot/szjzz/searchzzrk",
            ],
        )
        self.assertFalse(any(
            marker in request.url.path.lower()
            for request in requests
            for marker in ("save", "add", "delete", "cancel", "upload")
        ))

    async def test_http_failure_and_business_failure_never_become_first_registration(self):
        async def http_failure(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="fixture-sensitive-upstream-body")

        client = ResidencePlatformClient(
            config(),
            transport=httpx.MockTransport(http_failure),
        )
        with self.assertRaises(ResidencePlatformError) as raised:
            await client.lookup(VALID_IDENTITY)
        self.assertEqual(raised.exception.code, "http_error")
        self.assertNotIn("fixture-sensitive", str(raised.exception))

        async def business_failure(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/szjzz/searchIsck"):
                return httpx.Response(200, json={"success": True, "code": 200, "result": None})
            return httpx.Response(200, json={
                "success": False,
                "code": 500,
                "message": "虚构的其他业务错误",
                "result": None,
            })

        result = await ResidencePlatformClient(
            config(),
            transport=httpx.MockTransport(business_failure),
        ).lookup(VALID_IDENTITY)
        self.assertEqual((result.state, result.error_code), ("error", "business_error"))

    async def test_read_only_path_allowlist_rejects_other_routes(self):
        client = ResidencePlatformClient(config())
        with self.assertRaises(ResidencePlatformError) as raised:
            await client._post_readonly("/szjzz/save", {})
        self.assertEqual(raised.exception.code, "path_not_allowed")

    async def test_login_reads_mac_and_does_not_expose_upstream_rejection(self):
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "mac.invalid":
                return httpx.Response(200, json={"mac": "AA-BB-CC-DD-EE-FF"})
            body = json.loads(request.content.decode("utf-8"))
            self.assertEqual(body["mac"], "AA-BB-CC-DD-EE-FF")
            return httpx.Response(200, json={
                "success": False,
                "message": "fixture-sensitive-login-detail",
            })

        client = ResidencePlatformClient(
            config(access_token=""),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ResidencePlatformError) as raised:
            await client.login(captcha="1234", check_key="fixture-key")
        self.assertEqual(raised.exception.code, "login_rejected")
        self.assertNotIn("fixture-sensitive", str(raised.exception))
        self.assertEqual(len(requests), 2)

    async def test_login_fetches_hidden_challenge_and_submits_empty_captcha(self):
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "mac.invalid":
                return httpx.Response(200, json={"mac": "AA-BB-CC-DD-EE-FF"})
            if "/sys/randomImage/" in request.url.path:
                return httpx.Response(200, json={
                    "success": True,
                    "result": "data:image/png;base64,ZmFrZQ==",
                })
            body = json.loads(request.content.decode("utf-8"))
            self.assertEqual(body["captcha"], "")
            self.assertTrue(str(body["checkKey"]).isdigit())
            self.assertEqual(body["username"], "fixture-user")
            return httpx.Response(200, json={
                "success": True,
                "result": {
                    "token": "fixture-new-token",
                    "userInfo": {"orgCode": "999999999900"},
                },
            })

        token, organization_code = await ResidencePlatformClient(
            config(access_token=""),
            transport=httpx.MockTransport(handler),
        ).login()

        self.assertEqual(token, "fixture-new-token")
        self.assertEqual(organization_code, "999999999900")
        self.assertEqual(
            [request.method for request in requests],
            ["GET", "GET", "POST"],
        )


if __name__ == "__main__":
    unittest.main()
