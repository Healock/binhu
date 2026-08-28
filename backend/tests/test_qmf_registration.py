import asyncio
import base64
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from routers.qmf_registration import (
    QmfPreviewRequest,
    _record_preview_audit,
    preview_qmf_registration,
)
from services.qmf_registration import (
    QmfLoginContext,
    QmfLoginSession,
    QmfPreviewError,
    QmfReadOnlyClient,
    READ_ONLY_ENDPOINTS,
    _photo_payload,
    _safe_person,
    build_login_request,
    build_mid_local_request,
    build_timesync_request,
    open_login_session,
    parse_login_response,
    parse_mid_local_response,
    parse_timesync_response,
    preview_capability,
    preview_configured,
    reset_preview_guard_for_tests,
    run_guarded_preview,
    valid_identity,
)
from services.qmf_config import QmfRuntimeConfig


VALID_IDENTITY = "11010519491231002X"
JPEG = b"\xff\xd8\xff\xe0" + b"test-photo" + b"\xff\xd9"


def photo_json_response(photo: bytes = JPEG, *, with_whitespace: bool = False):
    encoded = base64.b64encode(photo).decode("ascii")
    if with_whitespace:
        encoded = "\r\n".join(encoded[index:index + 8] for index in range(0, len(encoded), 8))
    return httpx.Response(
        200,
        json={"code": 200, "message": "成功", "data": encoded},
        headers={"content-type": "application/json;charset=UTF-8"},
    )


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
    person_id=VALID_IDENTITY,
    person_sfzh="",
    person_community="1234567890",
    task_community="123456789012",
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
                "xfsq": task_community,
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
            person_data = {
                "name": person_name,
                "phone": "13000000000",
                "dz": "测试地址",
                "hjdzxz": "测试户籍地址",
                "gender": "女",
                "communityCode": person_community,
            }
            if person_id is not None:
                person_data["personID"] = person_id
            if person_sfzh:
                person_data["sfzh"] = person_sfzh
            return httpx.Response(200, json={
                "code": 200,
                "message": "success",
                "data": person_data,
            })
        if path.endswith("/enterHouse/queryPeoplePhotoByJzz"):
            return photo_json_response(photo)
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

    def test_person_codes_are_translated_and_birth_is_derived_from_valid_identity(self):
        person = _safe_person({
            "name": "测试甲",
            "personID": VALID_IDENTITY,
            "gender": "1",
            "nation": "01",
            "degree": "60",
            "hunyin": "1",
            "birth": "",
        })
        self.assertEqual(person["gender"], "男")
        self.assertEqual(person["nation"], "汉族")
        self.assertEqual(person["education"], "高中")
        self.assertEqual(person["marital_status"], "未婚")
        self.assertEqual(person["birth_date"], "1949-12-31")
        self.assertTrue(person["birth_date_derived"])
        self.assertEqual(person["gender_code"], "1")

    def test_unknown_numeric_person_code_is_not_guessed(self):
        person = _safe_person({
            "personID": VALID_IDENTITY,
            "gender": "7",
            "degree": "123",
        })
        self.assertEqual(person["gender"], "代码 7（待确认）")
        self.assertEqual(person["education"], "代码 123（待确认）")

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
        self.assertFalse(decoded.startswith("<?xml"))
        self.assertIn('type="request"', decoded)
        self.assertIn('module="base"', decoded)
        self.assertIn('platform="Android"', decoded)
        self.assertIn('id="IMEI"', decoded)
        self.assertIn('id="MACHINEUID"', decoded)

        response = (
            "<message type='response' seq='202608150700001' module='base'>"
            "<login>"
            "<parameters>"
            "<parameter id='MJXM' value='只读操作人'/>"
            "<parameter id='JGBM' value='320584710000'/>"
            "</parameters><modules/><services/>"
            "</login></message>"
        ).encode("gb2312")
        params = parse_login_response(response, expected_sequence="202608150700001")
        self.assertEqual(params["MJXM"], "只读操作人")

        identity_response = (
            "<message type='response' seq='202608150700003' module='MID_LOCAL'>"
            "<query><datas><data>"
            "<MJJH>operator-id</MJJH>"
            "<MJXM>只读操作人</MJXM>"
            "<JGBM>320584710000</JGBM>"
            "<JGMC>滨湖新城派出所</JGMC>"
            "</data></datas></query></message>"
        ).encode("gb2312")
        identity = parse_mid_local_response(
            identity_response, expected_sequence="202608150700003"
        )
        self.assertEqual(identity["MJJH"], "operator-id")

        timesync_response = (
            "<message type='response' seq='202608150700002' module='base'>"
            "<timesync to='2026-08-15 07:00:00'/></message>"
        ).encode("gb2312")
        self.assertEqual(
            parse_timesync_response(
                timesync_response, expected_sequence="202608150700002"
            ),
            "2026-08-15 07:00:00",
        )

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

    def test_preview_configuration_depends_on_connection_values(self):
        config = QmfRuntimeConfig(
            preview_enabled=False,
            registration_enabled=True,
            login_protocol_verified=False,
            write_protocol_verified=False,
            api_base_url="http://qmf.invalid/grid_terminal_interface/",
            login_host="qmf.invalid",
            login_port=25001,
            source_username="fictional-user",
            source_password="fictional-password",
            source_imei="fictional-imei",
            source_machine_uid="fictional-device",
            expected_station_code="320584710000",
            expected_station_name="滨湖新城派出所",
            timeout_seconds=15,
            session_max_seconds=45,
        )
        self.assertTrue(config.configured)
        self.assertTrue(config.registration_configured)
        self.assertTrue(preview_configured(config))
        self.assertFalse(preview_configured(QmfRuntimeConfig(
            **{**config.__dict__, "api_base_url": ""}
        )))
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

    def test_photo_validation_accepts_button_json_with_mime_base64_whitespace(self):
        result = _photo_payload(photo_json_response(with_whitespace=True))
        self.assertEqual(result["mime_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(result["data_base64"]), JPEG)

        with self.assertRaises(QmfPreviewError) as invalid:
            _photo_payload(httpx.Response(
                200,
                json={"code": 200, "message": "成功", "data": "not base64"},
                headers={"content-type": "application/json;charset=UTF-8"},
            ))
        self.assertEqual(invalid.exception.code, "photo_base64_invalid")

    async def test_preview_uses_verified_residence_permit_button_contract(self):
        captured: dict[str, str] = {}
        normal_handler, _seen = upstream_handler()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/enterHouse/queryPeoplePhotoByJzz"):
                captured["method"] = request.method
                captured["body"] = request.content.decode("ascii")
                captured["content_type"] = request.headers.get("content-type", "")
                return photo_json_response(with_whitespace=True)
            return await normal_handler(request)

        async def fake_login():
            return login_context()

        with (
            patch(
                "services.qmf_registration.settings.QMF_API_BASE_URL",
                "http://source.invalid/grid_terminal_interface/",
            ),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
        ):
            result = await QmfReadOnlyClient(
                transport=httpx.MockTransport(handler),
                login_provider=fake_login,
            ).preview(platform_task=platform_task())

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(
            captured["body"], f"personID={VALID_IDENTITY}&source=android"
        )
        self.assertTrue(captured["content_type"].startswith(
            "application/x-www-form-urlencoded"
        ))
        self.assertEqual(result["photo"]["mime_type"], "image/jpeg")

    def test_capability_requires_permission_and_eligible_source(self):
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
            allowed=True,
            parser_type="疑似未注销模型三",
            source_count=1,
            conflict=False,
            values=eligible,
        )
        self.assertTrue(allowed["enabled"])
        denied = preview_capability(
            allowed=False,
            parser_type="疑似未注销模型三",
            source_count=1,
            conflict=False,
            values=eligible,
        )
        self.assertFalse(denied["visible"])
        conflict = preview_capability(
            allowed=True,
            parser_type="疑似未注销模型三",
            source_count=2,
            conflict=True,
            values=eligible,
        )
        self.assertFalse(conflict["enabled"])

        for result in ("近期返吴", "离吴", "离开不返吴", "近期反吴"):
            with self.subTest(result=result):
                capability = preview_capability(
                    allowed=True,
                    parser_type="疑似未注销模型三",
                    source_count=1,
                    conflict=False,
                    values={"核查结果": result, "身份证号": VALID_IDENTITY},
                )
                self.assertTrue(capability["enabled"])

        for result in ("无法核实", "非本辖区"):
            with self.subTest(result=result):
                unsupported = preview_capability(
                    allowed=True,
                    parser_type="疑似未注销模型三",
                    source_count=1,
                    conflict=False,
                    values={"核查结果": result, "身份证号": VALID_IDENTITY},
                )
                self.assertFalse(unsupported["enabled"])

    async def test_preview_audit_records_only_safe_http_failure_context(self):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/qmf-registration/preview",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        audit = AsyncMock()
        with patch("routers.qmf_registration.record_admin_audit", audit):
            await _record_preview_audit(
                request=request,
                user={"id": 1, "username": "permission-user"},
                source_id=9,
                result="failed",
                started_at=time.monotonic(),
                error_code="upstream_http_error",
                error_step="query_photo",
                upstream_status=500,
                transport_error="read_timeout",
            )
        detail = audit.await_args.kwargs["detail"]
        self.assertEqual(detail["result_code"], "upstream_http_error")
        self.assertEqual(detail["error_step"], "query_photo")
        self.assertEqual(detail["upstream_http_status"], 500)
        self.assertEqual(detail["transport_error"], "read_timeout")
        self.assertNotIn("response", detail)
        self.assertNotIn("body", detail)

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
                user={"username": "permission-user"},
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
        self.assertEqual(result["upstream_task"]["community_code"], "123456789012")
        self.assertEqual(result["person"]["community_code"], "1234567890")
        self.assertTrue(result["checks"]["jurisdiction_match"])
        self.assertTrue(any("不同编码体系" in item for item in result["warnings"]))
        self.assertEqual(base64.b64decode(result["photo"]["data_base64"]), JPEG)
        self.assertEqual([method for method, _ in seen], ["POST", "POST", "POST", "POST"])
        allowed_suffixes = set(READ_ONLY_ENDPOINTS)
        self.assertIn("enterHouse/queryPeoplePhotoByJzz", allowed_suffixes)
        self.assertNotIn("jzz/queryLocalPhoto", allowed_suffixes)
        self.assertTrue(all(any(path.endswith("/" + suffix) for suffix in allowed_suffixes) for _, path in seen))
        for forbidden in ("uploadPhoto", "saveLocalPhoto", "addPeople", "fnmxCheck"):
            self.assertTrue(all(forbidden not in path for _, path in seen))

    async def test_readonly_query_uses_login_username_not_mid_local_operator_id(self):
        bodies = []
        handler, _seen = upstream_handler()

        async def wrapped(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content)
            return await handler(request)

        async def fake_login():
            return login_context()

        with (
            patch("services.qmf_registration.settings.QMF_API_BASE_URL", "http://source.invalid/grid_terminal_interface/"),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
            patch("services.qmf_registration.settings.QMF_EXPECTED_STATION_NAME", "滨湖新城派出所"),
        ):
            await QmfReadOnlyClient(
                transport=httpx.MockTransport(wrapped),
                login_provider=fake_login,
            ).preview(platform_task=platform_task())

        self.assertIn(b"mjjh=readonly-user", bodies[0])
        self.assertNotIn(b"mjjh=operator-id", bodies[0])

    async def test_readonly_request_retries_one_read_timeout_then_succeeds(self):
        handler, _seen = upstream_handler()
        task_attempts = 0

        async def flaky_handler(request: httpx.Request) -> httpx.Response:
            nonlocal task_attempts
            if request.url.path.endswith("/fnmx/queryYysList"):
                task_attempts += 1
                if task_attempts == 1:
                    raise httpx.ReadTimeout("timed out", request=request)
            return await handler(request)

        async def fake_login():
            return login_context()

        with (
            patch(
                "services.qmf_registration.settings.QMF_API_BASE_URL",
                "http://source.invalid/grid_terminal_interface/",
            ),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
            patch("services.qmf_registration.READ_ONLY_RETRY_DELAY_SECONDS", 0),
        ):
            result = await QmfReadOnlyClient(
                transport=httpx.MockTransport(flaky_handler),
                login_provider=fake_login,
            ).preview(platform_task=platform_task())

        self.assertEqual(task_attempts, 2)
        self.assertEqual(result["mode"], "read_only")

    async def test_readonly_request_reports_step_and_transport_after_retry(self):
        task_attempts = 0

        async def timeout_handler(request: httpx.Request) -> httpx.Response:
            nonlocal task_attempts
            task_attempts += 1
            raise httpx.ReadTimeout("sensitive timeout detail", request=request)

        async def fake_login():
            return login_context()

        with (
            patch(
                "services.qmf_registration.settings.QMF_API_BASE_URL",
                "http://source.invalid/grid_terminal_interface/",
            ),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
            patch("services.qmf_registration.READ_ONLY_RETRY_DELAY_SECONDS", 0),
            self.assertRaises(QmfPreviewError) as raised,
        ):
            await QmfReadOnlyClient(
                transport=httpx.MockTransport(timeout_handler),
                login_provider=fake_login,
            ).preview(platform_task=platform_task())

        self.assertEqual(task_attempts, 2)
        self.assertEqual(raised.exception.code, "upstream_unavailable")
        self.assertEqual(raised.exception.step, "query_task")
        self.assertEqual(raised.exception.transport_error, "read_timeout")
        self.assertIn("已自动重试一次", raised.exception.message)
        self.assertNotIn("sensitive timeout detail", raised.exception.message)

    async def test_readonly_request_retries_503_but_not_500(self):
        async def fake_login():
            return login_context()

        normal_handler, _seen = upstream_handler()
        task_attempts = 0

        async def service_unavailable_once(
            request: httpx.Request,
        ) -> httpx.Response:
            nonlocal task_attempts
            if request.url.path.endswith("/fnmx/queryYysList"):
                task_attempts += 1
                if task_attempts == 1:
                    return httpx.Response(503)
            return await normal_handler(request)

        with (
            patch(
                "services.qmf_registration.settings.QMF_API_BASE_URL",
                "http://source.invalid/grid_terminal_interface/",
            ),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
            patch("services.qmf_registration.READ_ONLY_RETRY_DELAY_SECONDS", 0),
        ):
            result = await QmfReadOnlyClient(
                transport=httpx.MockTransport(service_unavailable_once),
                login_provider=fake_login,
            ).preview(platform_task=platform_task())

        self.assertEqual(task_attempts, 2)
        self.assertEqual(result["mode"], "read_only")

        server_error_attempts = 0

        async def server_error(request: httpx.Request) -> httpx.Response:
            nonlocal server_error_attempts
            server_error_attempts += 1
            return httpx.Response(500, content=b"sensitive response body")

        with (
            patch(
                "services.qmf_registration.settings.QMF_API_BASE_URL",
                "http://source.invalid/grid_terminal_interface/",
            ),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
            patch("services.qmf_registration.READ_ONLY_RETRY_DELAY_SECONDS", 0),
            self.assertRaises(QmfPreviewError) as raised,
        ):
            await QmfReadOnlyClient(
                transport=httpx.MockTransport(server_error),
                login_provider=fake_login,
            ).preview(platform_task=platform_task())

        self.assertEqual(server_error_attempts, 1)
        self.assertEqual(raised.exception.code, "upstream_http_error")
        self.assertEqual(raised.exception.upstream_status, 500)
        self.assertNotIn("sensitive response body", raised.exception.message)

    async def test_login_retries_one_unavailable_session_then_succeeds(self):
        expected_session = object()
        first_error = QmfPreviewError(
            "login_unavailable",
            "temporary login failure",
            step="login",
            transport_error="connect_timeout",
        )
        open_once = AsyncMock(side_effect=[first_error, expected_session])
        with (
            patch(
                "services.qmf_registration._open_login_session_once",
                open_once,
            ),
            patch("services.qmf_registration.asyncio.sleep", AsyncMock()),
        ):
            session = await open_login_session()

        self.assertIs(session, expected_session)
        self.assertEqual(open_once.await_count, 2)

    async def test_login_reports_transport_after_retry_is_exhausted(self):
        open_once = AsyncMock(side_effect=[
            QmfPreviewError(
                "login_unavailable",
                "first sensitive failure",
                step="login",
                transport_error="connect_timeout",
            ),
            QmfPreviewError(
                "login_unavailable",
                "second sensitive failure",
                step="login",
                transport_error="read_timeout",
            ),
        ])
        with (
            patch(
                "services.qmf_registration._open_login_session_once",
                open_once,
            ),
            patch("services.qmf_registration.asyncio.sleep", AsyncMock()),
            self.assertRaises(QmfPreviewError) as raised,
        ):
            await open_login_session()

        self.assertEqual(open_once.await_count, 2)
        self.assertEqual(raised.exception.code, "login_unavailable")
        self.assertEqual(raised.exception.step, "login")
        self.assertEqual(raised.exception.transport_error, "read_timeout")
        self.assertIn("已自动重试一次", raised.exception.message)
        self.assertNotIn("sensitive failure", raised.exception.message)

    async def test_open_login_session_follows_apk_initialization_and_keeps_tcp_open(self):
        received = []
        server_ready = asyncio.Event()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                for index in range(3):
                    request = await reader.readuntil(b"</message>")
                    received.append(request.decode("gb18030"))
                    if index == 0:
                        response = (
                            "<message type='response' seq='202608150700001' module='base'>"
                            "<login><parameters>"
                            "<parameter id='MJXM' value='滨湖新城派出所'/>"
                            "<parameter id='JGBM' value='320584710000'/>"
                            "</parameters><modules/><services/></login></message>"
                        )
                    elif index == 1:
                        response = (
                            "<message type='response' seq='202608150700002' module='base'>"
                            "<timesync to='2026-08-15 07:00:00'/></message>"
                        )
                    else:
                        response = (
                            "<message type='response' seq='202608150700003' module='MID_LOCAL'>"
                            "<query><datas><data>"
                            "<MJJH>operator-id</MJJH>"
                            "<MJXM>滨湖新城派出所</MJXM>"
                            "<JGBM>320584710000</JGBM>"
                            "<JGMC>滨湖新城派出所</JGMC>"
                            "</data></datas></query></message>"
                        )
                    writer.write(response.encode("gb18030"))
                    await writer.drain()
                server_ready.set()
                await reader.read()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with (
                patch("services.qmf_registration.settings.QMF_LOGIN_HOST", "127.0.0.1"),
                patch("services.qmf_registration.settings.QMF_LOGIN_PORT", port),
                patch("services.qmf_registration.settings.QMF_SOURCE_USERNAME", "readonly-user"),
                patch("services.qmf_registration.settings.QMF_SOURCE_PASSWORD", "secret"),
                patch("services.qmf_registration.settings.QMF_SOURCE_IMEI", "authorized-imei"),
                patch("services.qmf_registration.settings.QMF_SOURCE_MACHINE_UID", "authorized-machine"),
                patch("services.qmf_registration.settings.QMF_EXPECTED_STATION_CODE", "320584710000"),
                patch("services.qmf_registration.settings.QMF_EXPECTED_STATION_NAME", "滨湖新城派出所"),
                patch("services.qmf_registration._login_sequence", return_value="202608150700001"),
            ):
                session = await open_login_session()
                self.assertFalse(session.writer.is_closing())
                self.assertEqual(session.context.operator_id, "operator-id")
                await server_ready.wait()
                self.assertIn('module="MID_LOCAL"', received[2])
                self.assertIn("readonly-user", received[2])
                await session.close()
        finally:
            server.close()
            await server.wait_closed()

    def test_login_session_deadline_fails_closed_without_guessing_heartbeat(self):
        session = QmfLoginSession(
            reader=None,
            writer=None,
            context=login_context(),
            started_at=time.monotonic() - 60,
        )
        with patch("services.qmf_registration.settings.QMF_SESSION_MAX_SECONDS", 45):
            with self.assertRaises(QmfPreviewError) as raised:
                session.ensure_available()
        self.assertEqual(raised.exception.code, "login_session_expired")

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
                    if expected_code == "upstream_http_error":
                        self.assertEqual(raised.exception.step, "query_task")
                        self.assertEqual(raised.exception.upstream_status, 503)
                        self.assertIn("任务查询", raised.exception.message)
                        self.assertIn("HTTP 503", raised.exception.message)

    async def test_photo_http_error_reports_safe_step_and_status(self):
        async def fake_login():
            return login_context()

        normal_handler, seen = upstream_handler()

        async def photo_error(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/enterHouse/queryPeoplePhotoByJzz"):
                return httpx.Response(
                    500,
                    content=b"sensitive photo backend detail",
                    headers={"content-type": "application/json"},
                )
            return await normal_handler(request)

        with (
            patch(
                "services.qmf_registration.settings.QMF_API_BASE_URL",
                "http://source.invalid/grid_terminal_interface/",
            ),
            patch("services.qmf_registration.settings.QMF_TIMEOUT_SECONDS", 5),
            self.assertRaises(QmfPreviewError) as raised,
        ):
            await QmfReadOnlyClient(
                transport=httpx.MockTransport(photo_error),
                login_provider=fake_login,
            ).preview(platform_task=platform_task())
        self.assertEqual(raised.exception.code, "upstream_http_error")
        self.assertEqual(raised.exception.step, "query_photo")
        self.assertEqual(raised.exception.upstream_status, 500)
        self.assertIn("居住证照片查询", raised.exception.message)
        self.assertIn("HTTP 500", raised.exception.message)
        self.assertNotIn("sensitive photo backend detail", raised.exception.message)
        self.assertEqual(
            seen,
            [
                ("POST", "/grid_terminal_interface/fnmx/queryYysList"),
                ("POST", "/grid_terminal_interface/enterHouse/queryPeopleBySfzh"),
            ],
        )

    async def test_task_person_station_and_image_mismatches_fail_closed(self):
        async def fake_login():
            return login_context()

        cases = (
            (upstream_handler(task_count=0)[0], "task_not_found"),
            (upstream_handler(task_count=2)[0], "task_not_unique"),
            (upstream_handler(person_name="其他人")[0], "person_mismatch"),
            (
                upstream_handler(
                    person_id=None,
                    person_sfzh=VALID_IDENTITY,
                )[0],
                "photo_person_mismatch",
            ),
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

    async def test_guard_rejects_concurrency_but_allows_next_manual_preview(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingClient:
            async def preview(self, *, platform_task):
                del platform_task
                entered.set()
                await release.wait()
                return {"mode": "read_only"}

        first = asyncio.create_task(run_guarded_preview(
            platform_task=platform_task(), client=BlockingClient()
        ))
        await entered.wait()
        with self.assertRaises(QmfPreviewError) as busy:
            await run_guarded_preview(platform_task=platform_task(), client=BlockingClient())
        self.assertEqual(busy.exception.code, "preview_busy")
        release.set()
        await first

        class ImmediateClient:
            async def preview(self, *, platform_task):
                del platform_task
                return {"mode": "read_only"}

        result = await run_guarded_preview(
            platform_task=platform_task(), client=ImmediateClient()
        )
        self.assertEqual(result["mode"], "read_only")


if __name__ == "__main__":
    unittest.main()
