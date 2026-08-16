import base64
import json
import unittest
from datetime import datetime
from urllib.parse import parse_qs

import httpx

from services.qmf_config import QmfRuntimeConfig
from services.qmf_registration import (
    ADD_PEOPLE_FIELDS,
    PERSONNEL_INFO_FIELDS,
    QmfCollectedContext,
    QmfLoginContext,
    QmfPreviewError,
    QmfRegistrationClient,
    RESULT_LEAVE_NOT_RETURNING,
    RESULT_RECENT_RETURN,
    build_add_people_payload,
    build_fnmx_check_payload,
    build_upload_photo_payload,
)


VALID_IDENTITY = "11010519491231002X"
JPEG = b"\xff\xd8\xff\xe0" + b"fictional-photo" + b"\xff\xd9"
PHOTO_SHA256 = "a" * 64


def photo_json_response(photo: bytes = JPEG):
    return httpx.Response(
        200,
        json={
            "code": 200,
            "message": "成功",
            "data": base64.b64encode(photo).decode("ascii"),
        },
        headers={"content-type": "application/json;charset=UTF-8"},
    )


def runtime_config() -> QmfRuntimeConfig:
    return QmfRuntimeConfig(
        preview_enabled=True,
        registration_enabled=True,
        login_protocol_verified=True,
        write_protocol_verified=True,
        preview_allowed_username="shenshenghua",
        api_base_url="http://qmf.invalid/grid_terminal_interface/",
        login_host="qmf.invalid",
        login_port=25001,
        source_username="fictional-operator",
        source_password="fictional-password",
        source_imei="fictional-device-001",
        source_machine_uid="Fictional Device",
        expected_station_code="320584710000",
        expected_station_name="滨湖新城派出所",
        timeout_seconds=5,
        session_max_seconds=45,
    )


def login_context() -> QmfLoginContext:
    return QmfLoginContext(
        username="fictional-operator",
        operator_id="fictional-operator-id",
        operator_name="滨湖新城派出所",
        station_code="320584710000",
        station_name="滨湖新城派出所",
    )


def raw_person() -> dict:
    person = {field: "" for field in PERSONNEL_INFO_FIELDS}
    person.update({
        "id": "fictional-person-record",
        "name": "测试人员甲",
        "personID": VALID_IDENTITY,
        "phone": "13000000000",
        "dz": "虚构现住址",
        "hjdzxz": "虚构户籍地址",
        "gender": "女",
        "birth": "19491231",
        "nation": "汉族",
        "degree": "本科",
        "hunyin": "未婚",
        "communityCode": "3205840001",
        "jzlx": "1",
        "jzfs": "1",
        "jzsy": "1",
        "sfzx": "1",
        "beizhu": "旧备注",
        "sbsbh": "旧设备",
    })
    return person


def platform_task() -> dict:
    return {
        "parser_type": "疑似未注销模型三",
        "row_key": "not-persisted-business-key",
        "source_id": 9,
        "name": "测试人员甲",
        "identity_number": VALID_IDENTITY,
        "phone": "13000000000",
        "address": "虚构现住址",
        "community": "虚构社区",
        "result": "在吴",
    }


def collected_context() -> QmfCollectedContext:
    person = raw_person()
    return QmfCollectedContext(
        platform_task=platform_task(),
        login_context=login_context(),
        query_data={"sfzh": VALID_IDENTITY},
        raw_task={},
        upstream_task={
            "task_id": "fictional-task-id",
            "record_id": "fictional-record-id",
            "name": "测试人员甲",
            "identity_number": VALID_IDENTITY,
            "phone": "13000000000",
            "address": "虚构现住址",
            "police_station": "滨湖新城派出所",
            "community": "虚构社区",
            "community_code": "320584123456",
            "check_status": "0",
            "check_status_text": "未核查",
            "dispatch_time": "2026-08-16 07:00:00",
        },
        raw_person=person,
        person={
            "name": "测试人员甲",
            "identity_number": VALID_IDENTITY,
            "phone": "13000000000",
            "current_address": "虚构现住址",
            "household_address": "虚构户籍地址",
            "gender": "女",
            "gender_code": "2",
            "birth_date": "19491231",
            "birth_date_derived": False,
            "nation": "汉族",
            "nation_code": "01",
            "education": "本科",
            "education_code": "20",
            "marital_status": "未婚",
            "marital_status_code": "1",
            "community_code": "3205840001",
            "residence_type": "1",
            "residence_method": "1",
            "residence_reason": "1",
            "active_status": "1",
        },
        photo={
            "mime_type": "image/jpeg",
            "size_bytes": len(JPEG),
            "sha256": PHOTO_SHA256,
            "data_base64": base64.b64encode(JPEG).decode("ascii"),
        },
    )


def leave_context() -> QmfCollectedContext:
    task = platform_task()
    task.update({
        "result": RESULT_LEAVE_NOT_RETURNING,
        "resolved_community": "虚构社区",
        "qmf_community_code": "3205840001",
        "destination_code": "510904",
        "destination_address": "四川省遂宁市安居区",
    })
    return QmfCollectedContext(
        platform_task=task,
        login_context=login_context(),
        query_data={"sfzh": VALID_IDENTITY},
        raw_task=upstream_task_row(),
        upstream_task={
            "task_id": "fictional-task-id",
            "record_id": "fictional-record-id",
            "name": "测试人员甲",
            "identity_number": VALID_IDENTITY,
            "phone": "13000000000",
            "address": "虚构现住址",
            "police_station": "滨湖新城派出所",
            "community": "虚构社区",
            "community_code": "320584123456",
            "check_status": "0",
            "check_status_text": "未核查",
            "dispatch_time": "2026-08-16 07:00:00",
        },
        raw_person=None,
        person=None,
        photo=None,
    )


def upstream_task_row() -> dict:
    return {
        "id": "fictional-record-id",
        "xfid": "fictional-task-id",
        "xm": "测试人员甲",
        "sfzh": VALID_IDENTITY,
        "lxfs": "13000000000",
        "dz": "虚构现住址",
        "pcsname": "滨湖新城派出所",
        "jgmc": "虚构社区",
        "xfsq": "320584123456",
        "hcjg": "0",
        "hcjgtext": "未核查",
        "xfsj": "2026-08-16 07:00:00",
    }


class QmfSingleRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def test_write_payloads_follow_confirmed_contract(self):
        context = collected_context()
        upload = build_upload_photo_payload(context)
        self.assertEqual(set(upload), {"csrq", "jmzh", "sfbljzz", "sqdm", "txsj", "xb", "xm"})
        self.assertEqual(upload["jmzh"], VALID_IDENTITY)
        self.assertEqual(base64.b64decode(upload["txsj"]), JPEG)

        add_people = build_add_people_payload(
            context,
            device_id="fictional-device-001",
            now=datetime(2026, 8, 16, 7, 0, 0),
        )
        self.assertEqual(set(add_people), ADD_PEOPLE_FIELDS)
        self.assertEqual(add_people["beizhu"], "滨湖新城派出所")
        self.assertEqual(add_people["operateBy"], "fictional-operator")
        self.assertEqual(add_people["operateType"], "2")
        self.assertEqual(add_people["sbsbh"], "fictional-device-001")
        self.assertEqual(add_people["djrq"], "20260816")
        self.assertEqual(
            add_people["mArrImgs"],
            [
                "/storage/emulated/0/.Wpa_Android_Base_WJ_Wgldpt/"
                f"fictional-operator/320584123456/Ry/Temp/{PHOTO_SHA256}.0"
            ],
        )

        feedback = build_fnmx_check_payload(
            context, device_id="fictional-device-001"
        )
        self.assertEqual(set(feedback), {
            "communityCode", "hcczr", "hcjg", "logoutReason", "personID",
            "qwdxz", "qwdxzqh", "sbsbh", "source", "type", "xfid",
        })
        self.assertEqual(feedback["hcjg"], "2")
        self.assertEqual(feedback["type"], "3")
        self.assertEqual(feedback["source"], "android")
        self.assertEqual(feedback["hcczr"], "fictional-operator")
        self.assertEqual(feedback["xfid"], "fictional-task-id")
        for field in ("communityCode", "logoutReason", "qwdxzqh", "qwdxz"):
            self.assertEqual(feedback[field], "")

    def test_recent_return_and_leave_feedback_contracts(self):
        recent = collected_context()
        recent.platform_task["result"] = RESULT_RECENT_RETURN
        recent_feedback = build_fnmx_check_payload(
            recent, device_id="fictional-device-001"
        )
        self.assertEqual(recent_feedback["hcjg"], "3")
        for field in ("communityCode", "logoutReason", "qwdxzqh", "qwdxz"):
            self.assertEqual(recent_feedback[field], "")

        leave_feedback = build_fnmx_check_payload(
            leave_context(), device_id="fictional-device-001"
        )
        self.assertEqual(list(leave_feedback), [
            "xfid", "logoutReason", "hcjg", "hcczr", "sbsbh", "personID",
            "type", "qwdxzqh", "communityCode", "qwdxz", "source",
        ])
        self.assertEqual(leave_feedback["hcjg"], "1")
        self.assertEqual(leave_feedback["logoutReason"], "2")
        self.assertEqual(leave_feedback["communityCode"], "3205840001")
        self.assertEqual(leave_feedback["qwdxzqh"], "510904")
        self.assertEqual(leave_feedback["qwdxz"], "四川省遂宁市安居区")
        self.assertNotIn("四川", leave_feedback["qwdxzqh"])

    async def test_leave_not_returning_executes_only_four_step_contract(self):
        seen: list[str] = []
        captured_feedback: dict[str, list[str]] = {}
        captured_community_query: dict[str, list[str]] = {}
        task_queries = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal task_queries, captured_community_query, captured_feedback
            path = request.url.path.split("/grid_terminal_interface/", 1)[-1]
            seen.append(path)
            if path == "fnmx/queryYysList":
                task_queries += 1
                rows = [upstream_task_row()] if task_queries == 1 else []
                return httpx.Response(200, json={
                    "code": 200,
                    "data": {"total": len(rows), "list": rows},
                })
            if path == "declare/queryCommunityCode":
                captured_community_query = parse_qs(
                    request.content.decode("utf-8"), keep_blank_values=True
                )
                return httpx.Response(200, json={"code": 200, "data": []})
            if path == "fnmx/fnmxCheck":
                captured_feedback = parse_qs(
                    request.content.decode("utf-8"), keep_blank_values=True
                )
                return httpx.Response(200, json={"code": 200, "data": {"ok": True}})
            raise AssertionError(f"leave contract reached forbidden endpoint: {path}")

        async def fake_login():
            return login_context()

        step_events: list[tuple[str, str]] = []

        async def step_callback(key: str, status: str, _code: str):
            step_events.append((key, status))

        result = await QmfRegistrationClient(
            transport=httpx.MockTransport(handler),
            login_provider=fake_login,
            config=runtime_config(),
        ).execute(
            platform_task=leave_context().platform_task,
            step_callback=step_callback,
            before_write=lambda *_args: _noop(),
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(seen, [
            "fnmx/queryYysList",
            "declare/queryCommunityCode",
            "fnmx/fnmxCheck",
            "fnmx/queryYysList",
        ])
        self.assertEqual(
            [key for key, status in step_events if status == "sending"],
            ["query_task", "query_community", "complete_task", "verify_final"],
        )
        self.assertEqual(captured_feedback["qwdxzqh"], ["510904"])
        self.assertEqual(captured_feedback["qwdxz"], ["四川省遂宁市安居区"])
        self.assertEqual(captured_community_query, {
            "sfzh": [VALID_IDENTITY],
            "source": ["android"],
        })
        for forbidden in (
            "queryPeopleBySfzh", "queryPeoplePhotoByJzz", "checkCk",
            "uploadPhoto", "saveLocalPhoto", "addPeople",
        ):
            self.assertTrue(all(forbidden not in path for path in seen))

    async def test_registration_executes_exact_nine_request_sequence(self):
        seen: list[str] = []
        captured: dict[str, object] = {}
        task_queries = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal task_queries
            path = request.url.path.split("/grid_terminal_interface/", 1)[-1]
            seen.append(path)
            if path == "fnmx/queryYysList":
                task_queries += 1
                rows = [upstream_task_row()] if task_queries == 1 else []
                return httpx.Response(200, json={
                    "code": 200,
                    "data": {"total": len(rows), "list": rows},
                })
            if path == "enterHouse/queryPeopleBySfzh":
                return httpx.Response(200, json={"code": 200, "data": raw_person()})
            if path == "enterHouse/queryPeoplePhotoByJzz":
                return photo_json_response()
            if path == "enterHouse/checkCk":
                return httpx.Response(200, json={"code": 200, "data": 0})
            if path == "masses/uploadPhoto":
                captured["upload"] = json.loads(request.content)
                captured["upload_content_type"] = request.headers.get("content-type")
                return httpx.Response(200, json={"code": 200, "data": {"ok": True}})
            if path == "jzz/saveLocalPhoto":
                captured["multipart"] = request.content
                captured["multipart_content_type"] = request.headers.get("content-type")
                return httpx.Response(200, json={"code": 200, "data": {"ok": True}})
            if path == "enterHouse/addPeople":
                captured["add_people"] = json.loads(request.content)
                captured["add_people_content_type"] = request.headers.get("content-type")
                return httpx.Response(200, json={"code": 200, "data": {"ok": True}})
            if path == "fnmx/fnmxCheck":
                captured["feedback"] = parse_qs(request.content.decode("utf-8"), keep_blank_values=True)
                captured["feedback_content_type"] = request.headers.get("content-type")
                return httpx.Response(200, json={"code": 200, "data": {"ok": True}})
            raise AssertionError(f"unexpected outbound path: {path}")

        async def fake_login():
            return login_context()

        step_events: list[tuple[str, str]] = []

        async def step_callback(key: str, status: str, _code: str):
            step_events.append((key, status))

        before_write_seen = []

        async def before_write(context: QmfCollectedContext):
            before_write_seen.append(context.upstream_task["task_id"])

        result = await QmfRegistrationClient(
            transport=httpx.MockTransport(handler),
            login_provider=fake_login,
            config=runtime_config(),
        ).execute(
            platform_task=platform_task(),
            step_callback=step_callback,
            before_write=before_write,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(before_write_seen, ["fictional-task-id"])
        self.assertEqual(seen, [
            "fnmx/queryYysList",
            "enterHouse/queryPeopleBySfzh",
            "enterHouse/queryPeoplePhotoByJzz",
            "enterHouse/checkCk",
            "masses/uploadPhoto",
            "jzz/saveLocalPhoto",
            "enterHouse/addPeople",
            "fnmx/fnmxCheck",
            "fnmx/queryYysList",
        ])
        self.assertEqual(
            [key for key, status in step_events if status == "sending"],
            [
                "query_task", "query_person", "query_photo", "precheck",
                "upload_photo", "save_local_photo", "register_person",
                "complete_task", "verify_final",
            ],
        )
        self.assertEqual(captured["upload"]["jmzh"], VALID_IDENTITY)
        self.assertTrue(str(captured["upload_content_type"]).startswith("application/json"))
        multipart = captured["multipart"]
        self.assertTrue(str(captured["multipart_content_type"]).startswith("multipart/form-data"))
        self.assertEqual(multipart.count(b'name="'), 4)
        self.assertIn(b'name="idCard"', multipart)
        self.assertIn(VALID_IDENTITY.encode("ascii"), multipart)
        self.assertIn(b'name="imageType"', multipart)
        self.assertIn(b"\r\n3\r\n", multipart)
        self.assertIn(b'name="label"', multipart)
        self.assertIn(b'name="createBy"', multipart)
        self.assertEqual(captured["add_people"]["beizhu"], "滨湖新城派出所")
        self.assertTrue(str(captured["add_people_content_type"]).startswith("application/json"))
        self.assertTrue(str(captured["feedback_content_type"]).startswith(
            "application/x-www-form-urlencoded"
        ))
        self.assertEqual(captured["feedback"]["communityCode"], [""])
        self.assertEqual(captured["feedback"]["qwdxzqh"], [""])

    async def test_person_schema_change_stops_before_all_write_endpoints(self):
        seen: list[str] = []
        incomplete_person = raw_person()
        incomplete_person.pop("alias")

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.split("/grid_terminal_interface/", 1)[-1]
            seen.append(path)
            if path == "fnmx/queryYysList":
                return httpx.Response(200, json={
                    "code": 200,
                    "data": {"total": 1, "list": [upstream_task_row()]},
                })
            if path == "enterHouse/queryPeopleBySfzh":
                return httpx.Response(200, json={"code": 200, "data": incomplete_person})
            if path == "enterHouse/queryPeoplePhotoByJzz":
                return photo_json_response()
            if path == "enterHouse/checkCk":
                return httpx.Response(200, json={"code": 200, "data": 0})
            raise AssertionError(f"write endpoint must not be reached: {path}")

        async def fake_login():
            return login_context()

        with self.assertRaises(QmfPreviewError) as raised:
            await QmfRegistrationClient(
                transport=httpx.MockTransport(handler),
                login_provider=fake_login,
                config=runtime_config(),
            ).execute(
                platform_task=platform_task(),
                step_callback=lambda *_args: _noop(),
                before_write=lambda *_args: _noop(),
            )
        self.assertEqual(raised.exception.code, "person_schema_changed")
        self.assertEqual(seen, [
            "fnmx/queryYysList",
            "enterHouse/queryPeopleBySfzh",
            "enterHouse/queryPeoplePhotoByJzz",
            "enterHouse/checkCk",
        ])

    async def test_precheck_requires_exact_verified_zero_result(self):
        for rejected_data in (1, "0", False, None, {"ok": True}):
            with self.subTest(rejected_data=rejected_data):
                seen: list[str] = []

                async def handler(request: httpx.Request) -> httpx.Response:
                    path = request.url.path.split(
                        "/grid_terminal_interface/", 1
                    )[-1]
                    seen.append(path)
                    if path == "fnmx/queryYysList":
                        return httpx.Response(200, json={
                            "code": 200,
                            "data": {"total": 1, "list": [upstream_task_row()]},
                        })
                    if path == "enterHouse/queryPeopleBySfzh":
                        return httpx.Response(
                            200, json={"code": 200, "data": raw_person()}
                        )
                    if path == "enterHouse/queryPeoplePhotoByJzz":
                        return photo_json_response()
                    if path == "enterHouse/checkCk":
                        return httpx.Response(
                            200, json={"code": 200, "data": rejected_data}
                        )
                    raise AssertionError(f"write endpoint must not be reached: {path}")

                async def fake_login():
                    return login_context()

                with self.assertRaises(QmfPreviewError) as raised:
                    await QmfRegistrationClient(
                        transport=httpx.MockTransport(handler),
                        login_provider=fake_login,
                        config=runtime_config(),
                    ).execute(
                        platform_task=platform_task(),
                        step_callback=lambda *_args: _noop(),
                        before_write=lambda *_args: _noop(),
                    )
                self.assertEqual(raised.exception.code, "precheck_rejected")
                self.assertEqual(seen[-1], "enterHouse/checkCk")

    async def test_write_timeout_is_uncertain_and_does_not_continue(self):
        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.split("/grid_terminal_interface/", 1)[-1]
            seen.append(path)
            if path == "fnmx/queryYysList":
                return httpx.Response(200, json={
                    "code": 200,
                    "data": {"total": 1, "list": [upstream_task_row()]},
                })
            if path == "enterHouse/queryPeopleBySfzh":
                return httpx.Response(200, json={"code": 200, "data": raw_person()})
            if path == "enterHouse/queryPeoplePhotoByJzz":
                return photo_json_response()
            if path == "enterHouse/checkCk":
                return httpx.Response(200, json={"code": 200, "data": 0})
            if path == "masses/uploadPhoto":
                raise httpx.ReadTimeout("fictional timeout", request=request)
            raise AssertionError(f"request after uncertain write: {path}")

        async def fake_login():
            return login_context()

        with self.assertRaises(QmfPreviewError) as raised:
            await QmfRegistrationClient(
                transport=httpx.MockTransport(handler),
                login_provider=fake_login,
                config=runtime_config(),
            ).execute(
                platform_task=platform_task(),
                step_callback=lambda *_args: _noop(),
                before_write=lambda *_args: _noop(),
            )
        self.assertEqual(raised.exception.code, "write_result_uncertain")
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(seen[-1], "masses/uploadPhoto")

    async def test_each_write_step_failure_stops_all_later_requests(self):
        write_paths = [
            "masses/uploadPhoto",
            "jzz/saveLocalPhoto",
            "enterHouse/addPeople",
            "fnmx/fnmxCheck",
        ]
        for failed_path in write_paths:
            for failure_mode in ("business", "timeout"):
                with self.subTest(failed_path=failed_path, failure_mode=failure_mode):
                    seen: list[str] = []
                    task_queries = 0

                    async def handler(request: httpx.Request) -> httpx.Response:
                        nonlocal task_queries
                        path = request.url.path.split(
                            "/grid_terminal_interface/", 1
                        )[-1]
                        seen.append(path)
                        if path == "fnmx/queryYysList":
                            task_queries += 1
                            rows = [upstream_task_row()] if task_queries == 1 else []
                            return httpx.Response(200, json={
                                "code": 200,
                                "data": {"total": len(rows), "list": rows},
                            })
                        if path == "enterHouse/queryPeopleBySfzh":
                            return httpx.Response(
                                200, json={"code": 200, "data": raw_person()}
                            )
                        if path == "enterHouse/queryPeoplePhotoByJzz":
                            return photo_json_response()
                        if path == "enterHouse/checkCk":
                            return httpx.Response(
                                200, json={"code": 200, "data": 0}
                            )
                        if path == failed_path:
                            if failure_mode == "timeout":
                                raise httpx.ReadTimeout(
                                    "fictional timeout", request=request
                                )
                            return httpx.Response(
                                200, json={"code": 409, "message": "fictional"}
                            )
                        if path in write_paths:
                            return httpx.Response(
                                200, json={"code": 200, "data": {"ok": True}}
                            )
                        raise AssertionError(f"unexpected outbound path: {path}")

                    async def fake_login():
                        return login_context()

                    with self.assertRaises(QmfPreviewError) as raised:
                        await QmfRegistrationClient(
                            transport=httpx.MockTransport(handler),
                            login_provider=fake_login,
                            config=runtime_config(),
                        ).execute(
                            platform_task=platform_task(),
                            step_callback=lambda *_args: _noop(),
                            before_write=lambda *_args: _noop(),
                        )
                    self.assertEqual(seen[-1], failed_path)
                    self.assertEqual(
                        raised.exception.uncertain,
                        failure_mode == "timeout",
                    )

    async def test_final_review_must_confirm_task_left_pending_queue(self):
        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.split("/grid_terminal_interface/", 1)[-1]
            seen.append(path)
            if path == "fnmx/queryYysList":
                return httpx.Response(200, json={
                    "code": 200,
                    "data": {"total": 1, "list": [upstream_task_row()]},
                })
            if path == "enterHouse/queryPeopleBySfzh":
                return httpx.Response(200, json={"code": 200, "data": raw_person()})
            if path == "enterHouse/queryPeoplePhotoByJzz":
                return photo_json_response()
            if path == "enterHouse/checkCk":
                return httpx.Response(200, json={"code": 200, "data": 0})
            if path in {
                "masses/uploadPhoto",
                "jzz/saveLocalPhoto",
                "enterHouse/addPeople",
                "fnmx/fnmxCheck",
            }:
                return httpx.Response(200, json={"code": 200, "data": {"ok": True}})
            raise AssertionError(f"unexpected outbound path: {path}")

        async def fake_login():
            return login_context()

        with self.assertRaises(QmfPreviewError) as raised:
            await QmfRegistrationClient(
                transport=httpx.MockTransport(handler),
                login_provider=fake_login,
                config=runtime_config(),
            ).execute(
                platform_task=platform_task(),
                step_callback=lambda *_args: _noop(),
                before_write=lambda *_args: _noop(),
            )
        self.assertEqual(raised.exception.code, "final_state_unconfirmed")
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(seen.count("fnmx/queryYysList"), 2)


async def _noop():
    return None


if __name__ == "__main__":
    unittest.main()
