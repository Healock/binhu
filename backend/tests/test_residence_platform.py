import asyncio
import base64
import json
import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.residence_platform import (  # noqa: E402
    ResidencePlatformClient,
    ResidencePlatformError,
    _photo_data_url,
    calculate_age,
    classify_floating_response,
    nation_label,
    registration_status,
)
from services.residence_platform_config import (  # noqa: E402
    ResidencePlatformConfig,
    public_residence_config,
    residence_username,
    serialize_residence_value,
)
from services.qmf_config import decrypt_secret  # noqa: E402
from services import residence_status_scan  # noqa: E402


VALID_IDENTITY = "11010519491231002X"


class FakeAcquire:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakePool:
    def acquire(self):
        return FakeAcquire()


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
        "full_scan_interval_minutes": 30,
    }
    values.update(overrides)
    return ResidencePlatformConfig(**values)


class ResidencePlatformTests(unittest.IsolatedAsyncioTestCase):
    def test_full_scan_interval_defaults_are_exposed(self):
        public = public_residence_config(config())
        self.assertEqual(public["full_scan_interval_minutes"], 30)

        scan_source = Path(__file__).parents[1].joinpath(
            "services", "residence_status_scan.py"
        ).read_text(encoding="utf-8")
        self.assertIn("queue_due_residence_tasks(force=full_scan)", scan_source)
        self.assertNotIn("REFRESH_DAYS", scan_source)

    def test_community_account_is_derived_from_qmf_code(self):
        self.assertEqual(residence_username("A123456789"), "A12345678900")
        with self.assertRaises(ValueError):
            residence_username("320584")

    def test_residence_status_schema_tracks_safe_total_duration(self):
        source = Path(__file__).parents[1].joinpath("services", "residence_status_scan.py").read_text(encoding="utf-8")
        self.assertIn("duration_ms INT UNSIGNED DEFAULT NULL", source)
        self.assertIn("time.perf_counter()", source)

    async def test_manual_full_scan_reports_safe_batch_progress(self):
        context = type("Context", (), {"update": AsyncMock()})()
        cycles = AsyncMock(side_effect=[
            {
                "processed": 2,
                "success_count": 2,
                "error_count": 0,
                "error_counts": {},
                "status": "completed",
            },
            {
                "processed": 1,
                "success_count": 0,
                "error_count": 1,
                "error_counts": {"request_error": 1},
                "status": "completed",
            },
            {"processed": 0, "status": "idle"},
        ])
        with patch.object(
            residence_status_scan,
            "_scan_lock",
            asyncio.Lock(),
        ), patch.object(
            residence_status_scan,
            "_pool",
            return_value=FakePool(),
        ), patch.object(
            residence_status_scan,
            "load_residence_config",
            new=AsyncMock(return_value=config()),
        ), patch.object(
            residence_status_scan,
            "queue_due_residence_tasks",
            new=AsyncMock(return_value=3),
        ), patch.object(
            residence_status_scan,
            "run_residence_lookup_cycle",
            new=cycles,
        ):
            result = await residence_status_scan.run_residence_full_scan_job(context)

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["error_counts"], {"request_error": 1})
        self.assertEqual(context.update.await_count, 3)
        self.assertEqual(context.update.await_args_list[-1].kwargs["current"], 3)
        self.assertEqual(context.update.await_args_list[-1].kwargs["total"], 3)
        for call in context.update.await_args_list:
            rendered = json.dumps(call.kwargs, ensure_ascii=False)
            self.assertNotIn(VALID_IDENTITY, rendered)
            self.assertNotIn("姓名", rendered)
            self.assertNotIn("地址", rendered)
        scan_tokens = {
            call.kwargs["scan_token"]
            for call in cycles.await_args_list
        }
        self.assertEqual(len(scan_tokens), 1)

    async def test_manual_full_scan_fails_safely_when_not_configured(self):
        context = type("Context", (), {"update": AsyncMock()})()
        with patch.object(
            residence_status_scan,
            "_scan_lock",
            asyncio.Lock(),
        ), patch.object(
            residence_status_scan,
            "_pool",
            return_value=FakePool(),
        ), patch.object(
            residence_status_scan,
            "load_residence_config",
            new=AsyncMock(return_value=config(enabled=False)),
        ):
            with self.assertRaisesRegex(RuntimeError, "配置尚未就绪"):
                await residence_status_scan.run_residence_full_scan_job(context)
        context.update.assert_not_awaited()

    def test_manual_scan_uses_tracked_deduplicated_job_and_shared_lock(self):
        backend = Path(__file__).parents[1]
        router_source = backend.joinpath("routers", "residence_platform.py").read_text(
            encoding="utf-8"
        )
        scan_source = backend.joinpath(
            "services", "residence_status_scan.py"
        ).read_text(encoding="utf-8")
        task_queue_source = backend.joinpath(
            "services", "admin_task_queue.py"
        ).read_text(encoding="utf-8")
        permission_source = backend.joinpath(
            "routers", "external_acquisition.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"residence_full_scan"', router_source)
        self.assertIn('dedupe_key="residence_full_scan"', router_source)
        self.assertIn('return {"run": run, "reused": reused}', router_source)
        self.assertGreaterEqual(scan_source.count("async with _scan_lock"), 2)
        self.assertIn('"residence_full_scan": "居住证登记状态全量查询"', task_queue_source)
        self.assertIn('kind == "residence_full_scan"', permission_source)
        self.assertIn("is_super_admin_user(user)", permission_source)

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

    async def test_lookup_detail_projects_whitelisted_fields_and_validates_photo(self):
        requests: list[httpx.Request] = []
        jpeg = b"\xff\xd8\xfffixture-jpeg"

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = json.loads(request.content.decode("utf-8"))
            if request.url.path.endswith("/szjzz/searchIsck"):
                self.assertEqual(body, {"sfzh": VALID_IDENTITY, "xzqh": "320584"})
                return httpx.Response(200, json={"success": True, "code": 200, "result": None})
            if request.url.path.endswith("/szjzz/searchzzrk"):
                return httpx.Response(200, json={
                    "success": True,
                    "code": 200,
                    "message": "查询成功",
                    "result": {
                        "birth": "invalid-upstream-birth",
                        "nation": "01",
                        "hjdz": "110105",
                        "hjdzxz": "虚构户籍地址",
                        "jlx_dictText": "虚构街道",
                        "mph": "88号",
                        "rysfzx": "0",
                        "rygxsj": "2026-08-20 08:00:00",
                        "fixture_secret_field": "must-not-be-returned",
                    },
                })
            if request.url.path.endswith("/szjzz/searchPhoto"):
                self.assertEqual(body, {"sfzh": VALID_IDENTITY})
                return httpx.Response(200, json={
                    "success": True,
                    "code": 200,
                    "result": {"图象数据": base64.b64encode(jpeg).decode("ascii")},
                })
            return httpx.Response(404)

        detail = await ResidencePlatformClient(
            config(),
            transport=httpx.MockTransport(handler),
        ).lookup_detail(VALID_IDENTITY)

        self.assertEqual(detail.birth_date, "1949-12-31")
        self.assertEqual(detail.ethnicity, "汉族")
        self.assertEqual(detail.registered_address, "虚构街道88号")
        self.assertEqual(detail.registration_status_text, "未注销")
        self.assertEqual(detail.photo_state, "available")
        self.assertTrue(detail.photo_data_url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(
            [request.url.path.rsplit("/grandlynn-boot", 1)[-1] for request in requests],
            ["/szjzz/searchIsck", "/szjzz/searchzzrk", "/szjzz/searchPhoto"],
        )
        self.assertFalse(hasattr(detail, "fixture_secret_field"))

    def test_demographic_and_registration_mappings_are_explicit(self):
        self.assertEqual(nation_label("1"), "汉族")
        self.assertEqual(nation_label("99"), "未说明民族")
        self.assertEqual(nation_label("88"), "民族代码 88")
        self.assertEqual(registration_status("0"), ("active", "未注销"))
        self.assertEqual(registration_status("1"), ("cancelled", "已注销"))
        self.assertEqual(registration_status("9"), ("unknown", "状态待核对"))

    def test_age_uses_birthday_boundary_and_handles_leap_day(self):
        self.assertEqual(calculate_age("2000-08-27", today=date(2026, 8, 27)), 26)
        self.assertEqual(calculate_age("2000-08-28", today=date(2026, 8, 27)), 25)
        self.assertEqual(calculate_age("2000-02-29", today=date(2026, 2, 28)), 25)
        self.assertEqual(calculate_age("2000-02-29", today=date(2026, 3, 1)), 26)
        self.assertIsNone(calculate_age("invalid", today=date(2026, 8, 27)))

    def test_photo_parser_accepts_png_whitespace_and_rejects_invalid_files(self):
        png = b"\x89PNG\r\n\x1a\nfixture-png"
        encoded = base64.b64encode(png).decode("ascii")
        spaced = f"{encoded[:6]}\r\n{encoded[6:]}"
        data_url, state, error_code = _photo_data_url({
            "success": True,
            "code": 200,
            "result": {"图象数据": spaced},
        })
        self.assertEqual((state, error_code), ("available", ""))
        self.assertTrue(data_url.startswith("data:image/png;base64,"))

        for raw, expected in (
            ("", ("missing", "")),
            ("not-base64", ("error", "photo_base64_invalid")),
            (base64.b64encode(b"plain text").decode("ascii"), ("error", "photo_type_invalid")),
        ):
            with self.subTest(raw=raw):
                _, actual_state, actual_error = _photo_data_url({
                    "success": True,
                    "code": 200,
                    "result": {"图象数据": raw},
                })
                self.assertEqual((actual_state, actual_error), expected)

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

    async def test_http_500_token_expiry_is_detected_safely(self):
        async def token_expired(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={
                "message": "Token失效，请重新登录",
                "detail": "fixture-sensitive-upstream-detail",
            })

        client = ResidencePlatformClient(
            config(),
            transport=httpx.MockTransport(token_expired),
        )
        with self.assertRaises(ResidencePlatformError) as raised:
            await client.lookup(VALID_IDENTITY)

        self.assertEqual(raised.exception.code, "authentication_expired")
        self.assertNotIn("fixture-sensitive", str(raised.exception))

    async def test_lookup_refreshes_expired_session_once(self):
        stale_client = type("Client", (), {})()
        stale_client.config = config(access_token="stale-token")
        stale_client.lookup = AsyncMock(side_effect=ResidencePlatformError(
            "authentication_expired",
            "居住证平台登录已失效",
        ))
        refreshed_result = object()
        fresh_client = type("Client", (), {})()
        fresh_client.lookup = AsyncMock(return_value=refreshed_result)
        community_client = AsyncMock(side_effect=[stale_client, fresh_client])
        target = residence_status_scan.ResidenceLookupTarget(
            identity=VALID_IDENTITY,
            community_code="3205840377",
        )

        with patch.object(
            residence_status_scan,
            "_community_client",
            new=community_client,
        ):
            result = await residence_status_scan._lookup_target(config(), target)

        self.assertIs(result, refreshed_result)
        self.assertEqual(community_client.await_count, 2)
        self.assertEqual(
            community_client.await_args_list[1].kwargs["rejected_token"],
            "stale-token",
        )
        stale_client.lookup.assert_awaited_once_with(VALID_IDENTITY)
        fresh_client.lookup.assert_awaited_once_with(VALID_IDENTITY)

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
