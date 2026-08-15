"""全民防模型三单条只读预演客户端。

本模块只暴露已通过静态分析和单条抓包确认的读取链路。任何新增上游
路径都必须先加入精确白名单并补充无写入测试；人员登记和模型反馈接口
故意不在客户端 API 中实现。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx

from config import settings


MODEL_THREE_PARSER = "疑似未注销模型三"
SUPPORTED_RESULT = "在吴"
ALLOWED_PLATFORM_USERNAME = "shenshenghua"
READ_ONLY_ENDPOINTS = {
    "fnmx/queryYysList": "POST",
    "enterHouse/queryPeopleBySfzh": "POST",
    "jzz/queryLocalPhoto": "GET",
    "enterHouse/checkCk": "POST",
}
MAX_LOGIN_RESPONSE_BYTES = 256 * 1024
MAX_PHOTO_BYTES = 5 * 1024 * 1024
_IDENTITY_PATTERN = re.compile(r"^\d{17}[0-9X]$")
_IDENTITY_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_IDENTITY_CHECKS = "10X98765432"


class QmfPreviewError(RuntimeError):
    """A safe, non-sensitive error suitable for an API response and audit."""

    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class QmfLoginContext:
    username: str
    operator_id: str
    operator_name: str
    station_code: str
    station_name: str


_preview_active = False
_last_preview_started = 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_identity(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value)).upper()


def valid_identity(value: Any) -> bool:
    """Validate an 18-digit PRC identity number including date and checksum."""
    identity = normalize_identity(value)
    if not _IDENTITY_PATTERN.fullmatch(identity):
        return False
    try:
        datetime.strptime(identity[6:14], "%Y%m%d")
    except ValueError:
        return False
    total = sum(int(char) * weight for char, weight in zip(
        identity[:17], _IDENTITY_WEIGHTS, strict=True
    ))
    return identity[-1] == _IDENTITY_CHECKS[total % 11]


def _normalized_name(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value))


def _station_matches(actual: Any, expected: Any) -> bool:
    return _normalized_name(actual) == _normalized_name(expected)


def preview_configured() -> bool:
    required = (
        settings.QMF_API_BASE_URL,
        settings.QMF_LOGIN_HOST,
        settings.QMF_LOGIN_PORT,
        settings.QMF_SOURCE_USERNAME,
        settings.QMF_SOURCE_PASSWORD,
        settings.QMF_SOURCE_IMEI,
        settings.QMF_SOURCE_MACHINE_UID,
        settings.QMF_EXPECTED_STATION_CODE,
        settings.QMF_EXPECTED_STATION_NAME,
    )
    return bool(
        settings.QMF_PREVIEW_ENABLED
        and settings.QMF_LOGIN_PROTOCOL_VERIFIED
        and settings.QMF_PREVIEW_ALLOWED_USERNAME == ALLOWED_PLATFORM_USERNAME
        and all(required)
    )


def preview_capability(
    *,
    username: str,
    parser_type: str,
    source_count: int,
    conflict: bool,
    values: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return server-computed visibility without exposing configuration values."""
    visible = (
        username == ALLOWED_PLATFORM_USERNAME
        and parser_type == MODEL_THREE_PARSER
    )
    if not visible:
        return {"visible": False, "enabled": False, "reason": ""}
    if not preview_configured():
        return {
            "visible": True,
            "enabled": False,
            "reason": "全民防只读预演尚未完成安全配置",
        }
    if conflict or source_count != 1 or not values:
        return {
            "visible": True,
            "enabled": False,
            "reason": "该任务不是唯一有效来源，不能预演",
        }
    if _text(values.get("核查结果")) != SUPPORTED_RESULT:
        return {
            "visible": True,
            "enabled": False,
            "reason": "第一版仅支持核查结果为“在吴”的任务",
        }
    if not valid_identity(values.get("身份证号")):
        return {
            "visible": True,
            "enabled": False,
            "reason": "任务身份证号格式无效，不能预演",
        }
    return {"visible": True, "enabled": True, "reason": ""}


def _login_sequence(now: datetime | None = None) -> str:
    current = now or datetime.now()
    return current.strftime("%Y%m%d%H%M%S") + "1"


def build_login_request(
    *, username: str, password: str, imei: str, machine_uid: str, sequence: str
) -> bytes:
    """Build the APK-compatible, GB2312 encoded login message."""
    message = ET.Element(
        "message",
        {"type": "request", "seq": sequence, "module": "base"},
    )
    login = ET.SubElement(
        message,
        "login",
        {
            "id": username,
            "passwd": password,
            "platform": "Android",
            "violent": "true",
        },
    )
    parameters = ET.SubElement(login, "parameters")
    ET.SubElement(parameters, "parameter", {"id": "IMEI", "value": imei})
    ET.SubElement(
        parameters,
        "parameter",
        {"id": "MACHINEUID", "value": machine_uid},
    )
    return ET.tostring(message, encoding="gb2312", xml_declaration=True)


def parse_login_response(payload: bytes, *, expected_sequence: str) -> dict[str, str]:
    if not payload or len(payload) > MAX_LOGIN_RESPONSE_BYTES:
        raise QmfPreviewError("login_response_invalid", "全民防登录响应结构无效")
    try:
        root = ET.fromstring(payload.decode("gb18030"))
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise QmfPreviewError(
            "login_response_invalid", "全民防登录响应结构无效"
        ) from exc
    if root.tag == "session":
        messages = [child for child in root if child.tag == "message"]
        if len(messages) != 1:
            raise QmfPreviewError("login_response_invalid", "全民防登录响应结构无效")
        root = messages[0]
    if (
        root.tag != "message"
        or root.attrib.get("type", "").lower() != "response"
        or root.attrib.get("seq") != expected_sequence
        or root.attrib.get("module") != "base"
    ):
        raise QmfPreviewError("login_response_invalid", "全民防登录响应校验失败")
    children = list(root)
    if len(children) != 1 or children[0].tag != "login":
        raise QmfPreviewError("login_response_invalid", "全民防登录响应结构无效")
    login = children[0]
    if _text(login.attrib.get("errcode")) != "0":
        raise QmfPreviewError("login_failed", "全民防登录失败", 502)
    parameters = login.find("parameters")
    if parameters is None:
        raise QmfPreviewError("login_response_invalid", "全民防登录响应缺少身份参数")
    result: dict[str, str] = {}
    for item in parameters.findall("parameter"):
        key = _text(item.attrib.get("id"))
        if key:
            if key in result:
                raise QmfPreviewError(
                    "login_response_invalid", "全民防登录响应包含重复身份参数"
                )
            result[key] = _text(item.attrib.get("value"))
    return result


def _login_context(parameters: dict[str, str]) -> QmfLoginContext:
    context = QmfLoginContext(
        username=settings.QMF_SOURCE_USERNAME,
        operator_id=_text(parameters.get("MJJH")),
        operator_name=_text(parameters.get("MJXM")),
        station_code=_text(parameters.get("JGBM")),
        station_name=_text(parameters.get("JGMC")),
    )
    if not all((
        context.operator_id,
        context.operator_name,
        context.station_code,
        context.station_name,
    )):
        raise QmfPreviewError("login_identity_missing", "全民防登录身份信息不完整")
    if (
        context.station_code != settings.QMF_EXPECTED_STATION_CODE
        or not _station_matches(
            context.station_name, settings.QMF_EXPECTED_STATION_NAME
        )
    ):
        raise QmfPreviewError("login_station_mismatch", "全民防登录机构不符合预演范围", 403)
    return context


async def login_readonly() -> QmfLoginContext:
    sequence = _login_sequence()
    request_bytes = build_login_request(
        username=settings.QMF_SOURCE_USERNAME,
        password=settings.QMF_SOURCE_PASSWORD,
        imei=settings.QMF_SOURCE_IMEI,
        machine_uid=settings.QMF_SOURCE_MACHINE_UID,
        sequence=sequence,
    )
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                settings.QMF_LOGIN_HOST,
                settings.QMF_LOGIN_PORT,
                limit=MAX_LOGIN_RESPONSE_BYTES,
            ),
            timeout=settings.QMF_TIMEOUT_SECONDS,
        )
        writer.write(request_bytes)
        await asyncio.wait_for(writer.drain(), timeout=settings.QMF_TIMEOUT_SECONDS)
        response = await asyncio.wait_for(
            reader.readuntil(b"</message>"),
            timeout=settings.QMF_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
        raise QmfPreviewError("login_unavailable", "全民防登录服务暂时不可用") from exc
    except asyncio.LimitOverrunError as exc:
        raise QmfPreviewError("login_response_too_large", "全民防登录响应超过安全限制") from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
    return _login_context(parse_login_response(response, expected_sequence=sequence))


def _business_payload(response: httpx.Response, *, expected_object: bool) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise QmfPreviewError("upstream_response_invalid", "全民防接口响应结构无效") from exc
    if not isinstance(payload, dict) or str(payload.get("code", "")) != "200":
        raise QmfPreviewError("upstream_business_error", "全民防接口返回业务错误")
    data = payload.get("data")
    if expected_object and not isinstance(data, dict):
        raise QmfPreviewError("upstream_response_invalid", "全民防接口响应结构无效")
    return data


def _photo_payload(response: httpx.Response) -> dict[str, Any]:
    content = response.content
    if not content or len(content) > MAX_PHOTO_BYTES:
        raise QmfPreviewError("photo_size_invalid", "居住证照片为空或超过安全大小限制")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        detected = "image/webp"
    else:
        raise QmfPreviewError("photo_type_invalid", "居住证照片格式校验失败")
    if content_type and content_type not in {detected, "application/octet-stream"}:
        raise QmfPreviewError("photo_type_mismatch", "居住证照片类型与文件内容不一致")
    return {
        "mime_type": detected,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "data_base64": base64.b64encode(content).decode("ascii"),
    }


def _first(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = _text(row.get(name))
        if value:
            return value
    return ""


def _safe_person(person: dict[str, Any]) -> dict[str, str]:
    """Project only fields required for the authenticated human comparison."""
    return {
        "name": _first(person, "name", "xm"),
        "identity_number": normalize_identity(_first(person, "personID", "sfzh")),
        "phone": _first(person, "phone", "lxfs", "wllxfs"),
        "current_address": _first(person, "dz", "address"),
        "household_address": _first(person, "hjdzxz", "hjdz"),
        "gender": _first(person, "gender", "xb"),
        "birth_date": _first(person, "birth", "csrq"),
        "nation": _first(person, "nation", "mz"),
        "education": _first(person, "degree", "whcd"),
        "marital_status": _first(person, "hunyin", "hyzk"),
        "community_code": _first(person, "communityCode", "sqdm"),
        "residence_type": _first(person, "jzlx"),
        "residence_method": _first(person, "jzfs"),
        "residence_reason": _first(person, "jzsy"),
        "active_status": _first(person, "sfzx"),
    }


def _safe_upstream_task(row: dict[str, Any]) -> dict[str, str]:
    return {
        "task_id": _first(row, "xfid"),
        "record_id": _first(row, "id"),
        "name": _first(row, "xm", "name"),
        "identity_number": normalize_identity(_first(row, "sfzh", "personID")),
        "phone": _first(row, "lxfs", "phone"),
        "address": _first(row, "dz", "address"),
        "police_station": _first(row, "pcsname"),
        "community": _first(row, "jgmc", "sqmc"),
        "community_code": _first(row, "xfsq"),
        "check_status": _first(row, "hcjg"),
        "check_status_text": _first(row, "hcjgtext"),
        "dispatch_time": _first(row, "xfsj", "createTime"),
    }


class QmfReadOnlyClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        login_provider: Callable[[], Any] = login_readonly,
    ):
        self._transport = transport
        self._login_provider = login_provider

    async def _request(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        method = READ_ONLY_ENDPOINTS.get(endpoint)
        if not method:
            raise QmfPreviewError("endpoint_not_allowed", "请求不在全民防只读白名单", 500)
        url = urljoin(settings.QMF_API_BASE_URL.rstrip("/") + "/", endpoint)
        try:
            response = await client.request(method, url, data=data, params=params)
            if not response.is_success:
                raise QmfPreviewError(
                    "upstream_http_error", "全民防只读接口返回 HTTP 错误"
                )
            return response
        except httpx.RequestError as exc:
            raise QmfPreviewError("upstream_unavailable", "全民防只读接口暂时不可用") from exc

    async def preview(
        self,
        *,
        platform_task: dict[str, Any],
    ) -> dict[str, Any]:
        identity = normalize_identity(platform_task.get("identity_number"))
        name = _text(platform_task.get("name"))
        if not valid_identity(identity):
            raise QmfPreviewError("identity_invalid", "任务身份证号格式无效", 422)
        login_context = await self._login_provider()
        if not isinstance(login_context, QmfLoginContext):
            raise QmfPreviewError("login_response_invalid", "全民防登录身份信息无效")

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        query_data = {
            "pcsbm": login_context.station_code,
            "sfzh": identity,
            "xm": "",
            "dz": "",
            "mjjh": login_context.operator_id,
            "hcjg": "",
            "cljg": "0",
            "pageSize": "10",
            "startTime": (now.date() - timedelta(days=92)).strftime("%Y%m%d"),
            "lxfs": "",
            "endTime": now.strftime("%Y%m%d"),
            "pageNum": "1",
            "source": "android",
        }
        timeout = httpx.Timeout(settings.QMF_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=self._transport,
            headers={"User-Agent": "Binhu-QMF-Readonly/0.20.1"},
        ) as client:
            task_response = await self._request(
                client, "fnmx/queryYysList", data=query_data
            )
            task_data = _business_payload(task_response, expected_object=True)
            raw_tasks = task_data.get("list")
            if not isinstance(raw_tasks, list):
                raise QmfPreviewError("task_response_invalid", "全民防任务响应结构无效")
            try:
                total_tasks = int(task_data.get("total", -1))
            except (TypeError, ValueError) as exc:
                raise QmfPreviewError(
                    "task_response_invalid", "全民防任务响应结构无效"
                ) from exc
            matching_tasks = [
                item for item in raw_tasks
                if isinstance(item, dict)
                and normalize_identity(_first(item, "sfzh", "personID")) == identity
            ]
            if total_tasks != 1 or len(matching_tasks) != 1:
                code = "task_not_found" if not matching_tasks else "task_not_unique"
                message = "全民防未找到唯一待处理任务"
                raise QmfPreviewError(code, message, 409)
            upstream_task = _safe_upstream_task(matching_tasks[0])
            if not upstream_task["task_id"] or not upstream_task["record_id"]:
                raise QmfPreviewError("task_identity_missing", "全民防任务标识不完整")
            if (
                upstream_task["identity_number"] != identity
                or _normalized_name(upstream_task["name"]) != _normalized_name(name)
            ):
                raise QmfPreviewError("task_person_mismatch", "全民防任务人员与平台任务不一致", 409)
            if not _station_matches(
                upstream_task["police_station"], settings.QMF_EXPECTED_STATION_NAME
            ):
                raise QmfPreviewError("task_station_mismatch", "全民防任务不属于目标派出所", 403)

            person_response = await self._request(
                client,
                "enterHouse/queryPeopleBySfzh",
                data={"sfzh": identity, "source": "android"},
            )
            raw_person = _business_payload(person_response, expected_object=True)
            person = _safe_person(raw_person)
            if (
                person["identity_number"] != identity
                or _normalized_name(person["name"]) != _normalized_name(name)
            ):
                raise QmfPreviewError("person_mismatch", "全民防人员登记资料与平台任务不一致", 409)
            if (
                not upstream_task["community_code"]
                or not person["community_code"]
                or upstream_task["community_code"] != person["community_code"]
            ):
                raise QmfPreviewError(
                    "person_jurisdiction_mismatch",
                    "全民防人员资料与任务辖区不一致",
                    409,
                )

            photo_response = await self._request(
                client,
                "jzz/queryLocalPhoto",
                params={"sfzh": identity, "timestamp": str(int(time.time() * 1000))},
            )
            photo = _photo_payload(photo_response)

            check_response = await self._request(
                client,
                "enterHouse/checkCk",
                data={"sfzh": identity, "source": "android"},
            )
            _business_payload(check_response, expected_object=False)

        return {
            "mode": "read_only",
            "can_submit": False,
            "platform_task": platform_task,
            "upstream_task": upstream_task,
            "person": person,
            "operator": {
                "username": login_context.username,
                "name": login_context.operator_name,
                "station_code": login_context.station_code,
                "station_name": login_context.station_name,
            },
            "photo": photo,
            "checks": {
                "source_revision": True,
                "single_source": True,
                "identity_match": True,
                "name_match": True,
                "single_upstream_task": True,
                "station_match": True,
                "person_match": True,
                "jurisdiction_match": True,
                "precheck_passed": True,
                "photo_valid": True,
            },
            "planned_write_steps": [
                {"key": "upload_photo", "label": "上传照片", "enabled": False},
                {"key": "save_local_photo", "label": "保存本地照片", "enabled": False},
                {"key": "register_person", "label": "保存人员登记", "enabled": False},
                {"key": "complete_task", "label": "完成模型三反馈", "enabled": False},
            ],
            "warnings": ["本页面仅用于人工核对，不会向全民防提交任何数据。"],
        }


async def run_guarded_preview(
    *,
    platform_task: dict[str, Any],
    client: QmfReadOnlyClient | None = None,
) -> dict[str, Any]:
    global _preview_active, _last_preview_started
    now = time.monotonic()
    cooldown = max(1, int(settings.QMF_PREVIEW_COOLDOWN_SECONDS))
    if _preview_active:
        raise QmfPreviewError("preview_busy", "已有一条全民防预演正在执行", 429)
    if _last_preview_started and now - _last_preview_started < cooldown:
        remaining = max(1, int(cooldown - (now - _last_preview_started)))
        raise QmfPreviewError(
            "preview_cooldown",
            f"请等待 {remaining} 秒后再执行下一次预演",
            429,
        )
    _preview_active = True
    _last_preview_started = now
    try:
        return await (client or QmfReadOnlyClient()).preview(
            platform_task=platform_task
        )
    finally:
        _preview_active = False


def reset_preview_guard_for_tests() -> None:
    global _preview_active, _last_preview_started
    _preview_active = False
    _last_preview_started = 0.0
