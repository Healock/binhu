import asyncio
import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from routers.qmf_registration import QmfPreviewRequest, preview_qmf_registration
from services.qmf_registration import (
    QmfLoginContext,
    QmfPreviewError,
    QmfReadOnlyClient,
    READ_ONLY_ENDPOINTS,
    _photo_payload,
    build_login_request,
    parse_login_response,
    preview_capability,
    preview_configured,
    reset_preview_guard_for_tests,
    run_guarded_preview,
    valid_identity,
)


VALID_IDENTITY = "11010519491231002X"
JPEG = b"\xff\xd8\xff\xe0" + b"test-photo" + b"\xff\xd9"


def login_context(station_name="滨湖新城派出所"):
    return QmfLoginContext(
        username="readonly-user",
        operator_id="operator-id",
        operator_name="只读操作人",
        station_code="320584710000",
        station_name=station_name,
    )


def platform_task():
    return {
        "parser_type": "疑似未注销模型三",
        "row_key": "internal-row-key",
        "source_id": 9,
        "name": "测试甲",
        "identity_number": VALID_IDENTITY,
        "phone": "13000000000",
        "address": "测试地址",
        "community": "测试社区",
        "result": "在吴",
    }


def upstream_handler(
    *,
    task_count=1,
    person_name="测试甲",
    person_community="community-code",
    station="滨湖新城派出所",
    photo=JPEG,
):
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/fnmx/queryYysList"):
            rows = [] if task_count == 0 else [{
                "id": "record-id",
                "xfid": "task-id",
                "xm": "测试甲",
                "sfzh": VALID_IDENTITY,
                "lxfs": "13000000000",
                "dz": "测试地址",
                "pcsname": station,
                "jgmc": "测试社区",
                "xfsq": "community-code",
                "hcjg": "0",
                "hcjgtext": "未核查",
                "xfsj": "2026-08-15 09:00:00",
            }]
            if task_count > 1:
                rows.append(dict(rows[0], id="record-id-2", xfid="task-id-2"))
            return httpx.Response(200, json={
                "code": 200,
                "message": "success",
                "data": {"total": task_count, "list": rows},
            })
        if path.endswith("/enterHouse/queryPeopleBySfzh"):
            return httpx.Response(200, json={
                "code": 200,
                "message": "success",
                "data": {
                    "name": person_name,
                    "personID": VALID_IDENTITY,
                    "phone": "13000000000",
                    "dz": "测试地址",
                    "hjdzxz": "测试户籍地址",
                    "gender": "女",
                    "communityCode": person_community,
                },
            })
        if path.endswith("/jzz/queryLocalPhoto"):
            return httpx.Response(200, content=photo, headers={"content-type": "image/jpeg"})
        if path.endswith("/enterHouse/checkCk"):
            return httpx.Response(200, json={"code": 200, "message": "success", "data": 0})
        raise AssertionError(f"unexpected outbound path: {path}")

    return handler, seen


class QmfRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_preview_guard_for_tests()

    def test_identity_validation_checks_date_and_checksum(self):
        self.assertTrue(valid_identity(VALID_IDENTITY))
        self.assertFalse(valid_identity("110105194912310020"))
        self.assertFalse(valid_identity("11010520260230002X"))

    def test_preview_request_rejects_browser_supplied_identity(self):
        with self.assertRaises(ValidationError):
            QmfPreviewRequest.model_validate({
                "parser_type": "疑似未注销模型三",
                "row_key": "row-key",
                "source_id": 1,
                "expected_revision": 1,
                "identity_number": VALID_IDENTITY,
            })

    def test_login_request_and_response_follow_apk_contract(self):
        request = build_login_request(
            username="fake-user",
            password="fake-password",
            imei="authorized-imei",
            machine_uid="authorized-machine",
            sequence="202608150700001",
        )
        decoded = request.decode("gb2312")
        self.assertIn('type="request"', decoded)
        self.assertIn('module="base"', decoded)
        self.assertIn('platform="Android"', decoded)
        self.assertIn('id="IMEI"', decoded)
        self.assertIn('id="MACHINEUID"', decoded)

        response = (
            "<message type='response' seq='202608150700001' module='base'>"
            "<login errcode='0' errmsg=''>"
            "<parameters>"
            "<parameter id='MJJH' value='operator-id'/>"
            "<parameter id='MJXM' value='只读操作人'/>"
            "<parameter id='JGBM' value='320584710000'/>"
            "<parameter id='JGMC' value='滨湖新城派出所'/>"
            "</parameters><modules/><services/>"
            "</login></message>"
        ).encode("gb2312")
        params = parse_login_response(response, expected_sequence="202608150700001")
        self.assertEqual(params["JGMC"], "滨湖新城派出所")

    def test_login_response_rejects_sequence_and_error_without_leaking_message(self):
        wrong_sequence = (
            "<message type='response' seq='other' module='base'>"
            "<login errcode='0'><parameters/></login></message>"
        ).encode()
        with self.assertRaises(QmfPreviewError) as raised:
            parse_login_response(wrong_sequence, expected_sequence="expected")
        self.assertEqual(raised.exception.code, "login_response_invalid")

        failure = (
            "<message type='response' seq='expected' module='base'>"
            "<login errcode='401' errmsg='raw credential detail'>"
            "<parameters/></login></message>"
        ).encode()
        with self.assertRaises(QmfPreviewError) as raised:
            parse_login_response(failure, expected_sequence="expected")
        self.assertEqual(raised.exception.code, "login_failed")
        self.assertNotIn("raw credential detail", raised.exception.message)

        duplicate = (
            "<message type='response' seq='expected' module='base'>"
            "<login errcode='0'><parameters>"
            "<parameter id='MJJH' value='first'/>"
            "<parameter id='MJJH' value='second'/>"
            "</parameters></login></message>"
        ).encode()
        with self.assertRaises(QmfPreviewError) as raised:
            parse_login_response(duplicate, expected_sequence="expected")
        self.assertEqual(raised.exception.code, "login_response_invalid")

    def test_preview_configuration_fails_closed(self):
        with patch("services.qmf_registration.settings.QMF_PREVIEW_ENABLED", False):
            self.assertFalse(preview_configured())
        with (
            patch("services.qmf_registration.settings.QMF_PREVIEW_ENABLED", True),
            patch("services.qmf_registration.settings.QMF_LOGIN_PROTOCOL_VERIFIED", False),
        ):
            self.assertFalse(preview_configured())
        with (
            patch("services.qmf_registration.settings.QMF_PREVIEW_ENABLED", True),
            patch("services.qmf_registration.settings.QMF_LOGIN_PROTOCOL_VERIFIED", True),
            patch("services.qmf_registration.settings.QMF_API_BASE_URL", ""),
        ):
            self.assertFalse(preview_configured())
        with (
            patch("services.qmf_registration.settings.QMF_PREVIEW_ENABLED", True),
            patch("services.qmf_registration.settings.QMF_LOGIN_PROTOCOL_VERIFIED", True),
            patch("services.qmf_registration.settings.QMF_PREVIEW_ALLOWED_USERNAME", "admin"),
        ):
            self.assertFalse(preview_configured())

    def test_photo_validation_accepts_supported_headers_and_rejects_unsafe_content(self):
        cases = (
            (b"\xff\xd8\xffsample", "image/jpeg"),
            (b"\x89PNG\r\n\x1a\nsample", "image/png"),
            (b"RIFF\x04\x00\x00\x00WEBPsample", "image/webp"),
        )
        for content, mime_type in cases:
            with self.subTest(mime_type=mime_type):
                result = _photo_payload(httpx.Response(
                    200, content=content, headers={"content-type": mime_type}
                ))
                self.assertEqual(result["mime_type"], mime_type)
                self.assertEqual(base64.b64decode(result["data_base64"]), content)

        with self.assertRaises(QmfPreviewError) as mismatch:
            _photo_payload(httpx.Response(
                200, content=JPEG, headers={"content-type": "image/png"}
            ))
        self.assertEqual(mismatch.exception.code, "photo_type_mismatch")

        with patch("services.qmf_registration.MAX_PHOTO_BYTES", 4):
            with self.assertRaises(QmfPreviewError) as oversized:
                _photo_payload(httpx.Response(
                    200, content=JPEG, headers={"content-type": "image/jpeg"}
                ))
        self.assertEqual(oversized.exception.code, "photo_size_invalid")

    def test_capability_is_exact_account_only_and_requires_eligible_source(self):
        patches = (
            patch("services.qmf_registration.settings.QMF_PREVIEW_ENABLED", True),
            patch("services.qmf_registration.settings.QMF_LOGIN_PROTOCOL_VERIFIED", True),
            patch("services.qmf_registration.settings.QMF_API_BASE_URL", "http://source.invalid/grid_terminal_interface/"),
            patch("services.qmf_registration.settings.QMF_LOGIN_HOST", "source.invalid"),
            patch("services.qmf_registration.settings.QMF_LOGIN_PORT", 1234),
            patch("services.qmf_registration.settings.QMF_SOURCE_USERNAME", "reader"),
            patch("services.qmf_registration.settings.QMF_SOURCE_PASSWORD", "secret"),
            patch("services.qmf_registration.settings.QMF_SOURCE_IMEI", "imei"),
            patch("services.qmf_registration.settings.QMF_SOURCE_MACHINE_UID", "machine"),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        eligible = {"核查结果": "在吴", "身份证号": VALID_IDENTITY}
        allowed = preview_capability(
            username="shenshenghua",
            parser_type="疑似未注销模型三",
            source_count=1,
            conflict=False,
            values=eligible,
        )
        self.assertTrue(allowed["enabled"])
        super_admin = preview_capability(
            username="admin",
            parser_type="疑似未注销模型三",
            source_count=1,
            conflict=False,
            values=eligible,
        )
        self.assertFalse(super_admin["visible"])
        conflict = preview_capability(
            username="shenshenghua",
            parser_type="疑似未注销模型三",
            source_count=2,
            conflict=True,
            values=eligible,
        )
        self.assertFalse(conflict["enabled"])

    async def test_route_rejects_super_admin_account_before_database_or_network(self):
        request = Request({"type": "http", "method": "POST", "path": "/api/qmf-registration/preview", "headers": []})
        with self.assertRaises(HTTPException) as raised:
            await preview_qmf_registration(
                QmfPreviewRequest(
                    parser_type="疑似未注销模型三",
                    row_key="row-key",
                    source_id=1,
                    expected_revision=1,
                ),
                request,
                user={"username": "super-admin", "role": "super_admin"},
                conn=None,
            )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_route_revalidates_source_before_and_after_upstream_read(self):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/qmf-registration/preview",
            "headers": [],
        })
        values = {
            "姓名": "测试甲",
            "身份证号": VALID_IDENTITY,
            "联系方式": "13000000000",
            "地址": "测试地址",
            "下发社区": "测试社区",
            "核查结果": "在吴",
        }
        detail = {
            "task": {"conflict": False},
            "sources": [{
                "id": 9,
                "revision": 3,
                "row_hash": "safe-row-hash",
                "values": values,
            }],
        }
        result = {
            "mode": "read_only",
            "can_submit": False,
            "photo": {
                "mime_type": "image/jpeg",
                "size_bytes": len(JPEG),
                "sha256": "a" * 64,
                "data_base64": base64.b64encode(JPEG).decode(),
            },
        }
        source_check = AsyncMock()
        with (
            patch("routers.qmf_registration.preview_configured", return_value=True),
            patch("routers.qmf_registration._mobile_task_detail_data", AsyncMock(return_value=detail)),
            patch("routers.qmf_registration.source_row_hash", return_value="safe-row-hash"),
            patch("routers.qmf_registration._assert_source_unchanged", source_check),
            patch("routers.qmf_registration.run_guarded_preview", AsyncMock(return_value=result)),
            patch("routers.qmf_registration._record_preview_audit", AsyncMock()),
        ):
            response = await preview_qmf_registration(
                QmfPreviewRequest(
                    parser_type="疑似未注销模型三",
                    row_key="row-key",
                    source_id=9,
                    expected_revision=3,
                ),
                request,
                user={"username": "shenshenghua"},
                conn=object(),
            )
        self.assertEqual(source_check.await_count, 2)
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        self.assertFalse(json.loads(response.body)["can_submit"])

    async def test_readonly_preview_uses_only_four_whitelisted_requests(self):
        handler, seen = upstream_handler()

        async def fake_login():
            return login_context()

        with (
            patch("services.qmf_registration.settings.QMF_API_BASE_URL", "http://source.invalid/grid_terminal_interface/"),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
            patch("services.qmf_registration.settings.QMF_EXPECTED_STATION_NAME", "滨湖新城派出所"),
        ):
            result = await QmfReadOnlyClient(
                transport=httpx.MockTransport(handler),
                login_provider=fake_login,
            ).preview(platform_task=platform_task())

        self.assertEqual(result["mode"], "read_only")
        self.assertFalse(result["can_submit"])
        self.assertEqual(result["person"]["identity_number"], VALID_IDENTITY)
        self.assertEqual(base64.b64decode(result["photo"]["data_base64"]), JPEG)
        self.assertEqual([method for method, _ in seen], ["POST", "POST", "GET", "POST"])
        allowed_suffixes = set(READ_ONLY_ENDPOINTS)
        self.assertTrue(all(any(path.endswith("/" + suffix) for suffix in allowed_suffixes) for _, path in seen))
        for forbidden in ("uploadPhoto", "saveLocalPhoto", "addPeople", "fnmxCheck"):
            self.assertTrue(all(forbidden not in path for _, path in seen))

    async def test_upstream_http_and_business_errors_do_not_leak_response_body(self):
        async def fake_login():
            return login_context()

        async def http_error(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, content=b"sensitive upstream body")

        async def business_error(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "code": 500,
                "message": "sensitive upstream detail",
                "data": None,
            })

        with (
            patch("services.qmf_registration.settings.QMF_API_BASE_URL", "http://source.invalid/grid_terminal_interface/"),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
        ):
            for handler, expected_code, secret in (
                (http_error, "upstream_http_error", "sensitive upstream body"),
                (business_error, "upstream_business_error", "sensitive upstream detail"),
            ):
                with self.subTest(expected_code=expected_code):
                    with self.assertRaises(QmfPreviewError) as raised:
                        await QmfReadOnlyClient(
                            transport=httpx.MockTransport(handler),
                            login_provider=fake_login,
                        ).preview(platform_task=platform_task())
                    self.assertEqual(raised.exception.code, expected_code)
                    self.assertNotIn(secret, raised.exception.message)

    async def test_task_person_station_and_image_mismatches_fail_closed(self):
        async def fake_login():
            return login_context()

        cases = (
            (upstream_handler(task_count=0)[0], "task_not_found"),
            (upstream_handler(task_count=2)[0], "task_not_unique"),
            (upstream_handler(person_name="其他人")[0], "person_mismatch"),
            (upstream_handler(person_community="other-community")[0], "person_jurisdiction_mismatch"),
            (upstream_handler(station="其他派出所")[0], "task_station_mismatch"),
            (upstream_handler(photo=b"not-an-image")[0], "photo_type_invalid"),
        )
        with (
            patch("services.qmf_registration.settings.QMF_API_BASE_URL", "http://source.invalid/grid_terminal_interface/"),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
            patch("services.qmf_registration.settings.QMF_EXPECTED_STATION_NAME", "滨湖新城派出所"),
        ):
            for handler, expected_code in cases:
                with self.subTest(expected_code=expected_code):
                    with self.assertRaises(QmfPreviewError) as raised:
                        await QmfReadOnlyClient(
                            transport=httpx.MockTransport(handler),
                            login_provider=fake_login,
                        ).preview(platform_task=platform_task())
                    self.assertEqual(raised.exception.code, expected_code)

    async def test_guard_rejects_concurrency_and_enforces_cooldown(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingClient:
            async def preview(self, *, platform_task):
                del platform_task
                entered.set()
                await release.wait()
                return {"mode": "read_only"}

        with patch("services.qmf_registration.settings.QMF_PREVIEW_COOLDOWN_SECONDS", 30):
            first = asyncio.create_task(run_guarded_preview(
                platform_task=platform_task(), client=BlockingClient()
            ))
            await entered.wait()
            with self.assertRaises(QmfPreviewError) as busy:
                await run_guarded_preview(platform_task=platform_task(), client=BlockingClient())
            self.assertEqual(busy.exception.code, "preview_busy")
            release.set()
            await first
            with self.assertRaises(QmfPreviewError) as cooldown:
                await run_guarded_preview(platform_task=platform_task(), client=BlockingClient())
            self.assertEqual(cooldown.exception.code, "preview_cooldown")


if __name__ == "__main__":
    unittest.main()
