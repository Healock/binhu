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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx

from config import settings  # kept as a compatibility patch target for tests
from services.qmf_config import QmfRuntimeConfig, settings_config


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


@dataclass
class QmfLoginSession:
    """A short-lived APK-compatible TCP login session.

    The old client keeps this connection alive while it performs HTTP reads.
    We intentionally do not invent a heartbeat frame here: one preview is
    bounded below the observed 60-second heartbeat interval, and an unknown
    heartbeat format must fail closed rather than be guessed.
    """

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    context: QmfLoginContext
    started_at: float
    config: QmfRuntimeConfig = field(default_factory=settings_config)

    def ensure_available(self) -> None:
        max_seconds = max(1, int(self.config.session_max_seconds))
        if time.monotonic() - self.started_at >= max_seconds:
            raise QmfPreviewError(
                "login_session_expired",
                "全民防登录会话超过只读预演安全时限",
                502,
            )

    async def close(self) -> None:
        if self.writer.is_closing():
            return
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass

    async def __aenter__(self) -> "QmfLoginSession":
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self.close()


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


def preview_configured(config: QmfRuntimeConfig | None = None) -> bool:
    return (config or settings_config()).configured


def preview_capability(
    *,
    username: str,
    parser_type: str,
    source_count: int,
    conflict: bool,
    values: dict[str, Any] | None,
    config: QmfRuntimeConfig | None = None,
) -> dict[str, Any]:
    """Return server-computed visibility without exposing configuration values."""
    visible = (
        username == ALLOWED_PLATFORM_USERNAME
        and parser_type == MODEL_THREE_PARSER
    )
    if not visible:
        return {"visible": False, "enabled": False, "reason": ""}
    if not preview_configured(config):
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
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return current.strftime("%Y%m%d%H%M%S") + "1"


def build_login_request(
    *, username: str, password: str, imei: str, machine_uid: str, sequence: str
) -> bytes:
    """Build the APK-compatible login message.

    The captured APK request has no XML declaration.  Its non-ASCII bytes are
    in the GB2312-compatible subset; GB18030 is the strict superset used by
    the receiver so we do not add an encoding declaration the APK never sent.
    """
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
    return ET.tostring(message, encoding="gb18030", xml_declaration=False)


def _encode_xml(message: ET.Element) -> bytes:
    return ET.tostring(message, encoding="gb18030", xml_declaration=False)


def build_timesync_request(*, sequence: str) -> bytes:
    message = ET.Element(
        "message",
        {"type": "request", "seq": sequence, "module": "base"},
    )
    ET.SubElement(message, "timesync")
    return _encode_xml(message)


def build_mid_local_request(*, username: str, sequence: str) -> bytes:
    message = ET.Element(
        "message",
        {"type": "request", "seq": sequence, "module": "MID_LOCAL"},
    )
    query = ET.SubElement(message, "query", {"p": "qid=QID_D_LOCAL|page=1|size=8|"})
    meta = ET.SubElement(query, "meta")
    ET.SubElement(meta, "ZDDM", {"p": ""})
    ET.SubElement(meta, "MJJH", {"p": ""}).text = username
    return _encode_xml(message)


def _next_sequence(sequence: str) -> str:
    try:
        return str(int(sequence) + 1).zfill(len(sequence))
    except ValueError as exc:
        raise QmfPreviewError("login_sequence_invalid", "全民防登录序号无效") from exc


def _parse_message(
    payload: bytes, *, expected_sequence: str, expected_module: str
) -> ET.Element:
    if not payload or len(payload) > MAX_LOGIN_RESPONSE_BYTES:
        raise QmfPreviewError("login_response_invalid", "全民防登录响应结构无效")
    try:
        root = ET.fromstring(payload.decode("gb18030"))
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise QmfPreviewError(
            "login_response_invalid", "全民防登录响应结构无效"
        ) from exc
    if (
        root.tag != "message"
        or root.attrib.get("type", "").lower() != "response"
        or root.attrib.get("seq") != expected_sequence
        or root.attrib.get("module") != expected_module
    ):
        raise QmfPreviewError("login_response_invalid", "全民防登录响应校验失败")
    return root


def _parameter_values(parameters: ET.Element | None) -> dict[str, str]:
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


def parse_login_response(payload: bytes, *, expected_sequence: str) -> dict[str, str]:
    root = _parse_message(
        payload, expected_sequence=expected_sequence, expected_module="base"
    )
    login = root.find("login")
    if login is None:
        raise QmfPreviewError("login_response_invalid", "全民防登录响应结构无效")
    errcode = _text(login.attrib.get("errcode"))
    if errcode and errcode != "0":
        raise QmfPreviewError("login_failed", "全民防登录失败", 502)
    return _parameter_values(login.find("parameters"))


def parse_timesync_response(payload: bytes, *, expected_sequence: str) -> str:
    root = _parse_message(
        payload, expected_sequence=expected_sequence, expected_module="base"
    )
    timesync = root.find("timesync")
    target = _text(timesync.attrib.get("to")) if timesync is not None else ""
    if timesync is None or not target:
        raise QmfPreviewError("login_response_invalid", "全民防时间同步响应结构无效")
    return target


def parse_mid_local_response(payload: bytes, *, expected_sequence: str) -> dict[str, str]:
    root = _parse_message(
        payload, expected_sequence=expected_sequence, expected_module="MID_LOCAL"
    )
    query = root.find("query")
    data_nodes = query.findall("datas/data") if query is not None else []
    if len(data_nodes) != 1:
        raise QmfPreviewError("login_response_invalid", "全民防身份响应结构无效")
    data = data_nodes[0]
    values: dict[str, str] = {}
    for child in list(data):
        if child.tag in values:
            raise QmfPreviewError("login_response_invalid", "全民防身份响应包含重复字段")
        values[child.tag] = _text(child.text)
    return values


def _login_context(
    base_parameters: dict[str, str],
    identity_parameters: dict[str, str],
    config: QmfRuntimeConfig,
) -> QmfLoginContext:
    if base_parameters.get("JGBM") and identity_parameters.get("JGBM"):
        if base_parameters["JGBM"] != identity_parameters["JGBM"]:
            raise QmfPreviewError("login_response_invalid", "全民防登录机构字段不一致")
    if base_parameters.get("MJXM") and identity_parameters.get("MJXM"):
        if base_parameters["MJXM"] != identity_parameters["MJXM"]:
            raise QmfPreviewError("login_response_invalid", "全民防登录身份字段不一致")
    context = QmfLoginContext(
        username=config.source_username,
        operator_id=_text(identity_parameters.get("MJJH")),
        operator_name=_text(identity_parameters.get("MJXM")),
        station_code=_text(identity_parameters.get("JGBM")),
        station_name=_text(identity_parameters.get("JGMC")),
    )
    if not all((
        context.operator_id,
        context.operator_name,
        context.station_code,
        context.station_name,
    )):
        raise QmfPreviewError("login_identity_missing", "全民防登录身份信息不完整")
    if (
        context.station_code != config.expected_station_code
        or not _station_matches(
            context.station_name, config.expected_station_name
        )
    ):
        raise QmfPreviewError("login_station_mismatch", "全民防登录机构不符合预演范围", 403)
    return context


async def open_login_session(config: QmfRuntimeConfig | None = None) -> QmfLoginSession:
    config = config or settings_config()
    sequence = _login_sequence()
    request_bytes = build_login_request(
        username=config.source_username,
        password=config.source_password,
        imei=config.source_imei,
        machine_uid=config.source_machine_uid,
        sequence=sequence,
    )
    writer: asyncio.StreamWriter | None = None
    session: QmfLoginSession | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                config.login_host,
                config.login_port,
                limit=MAX_LOGIN_RESPONSE_BYTES,
            ),
            timeout=config.timeout_seconds,
        )
        writer.write(request_bytes)
        await asyncio.wait_for(writer.drain(), timeout=config.timeout_seconds)
        base_response = await asyncio.wait_for(
            reader.readuntil(b"</message>"),
            timeout=config.timeout_seconds,
        )
        base_parameters = parse_login_response(
            base_response, expected_sequence=sequence
        )

        timesync_sequence = _next_sequence(sequence)
        writer.write(build_timesync_request(sequence=timesync_sequence))
        await asyncio.wait_for(writer.drain(), timeout=config.timeout_seconds)
        timesync_response = await asyncio.wait_for(
            reader.readuntil(b"</message>"),
            timeout=config.timeout_seconds,
        )
        parse_timesync_response(
            timesync_response, expected_sequence=timesync_sequence
        )

        identity_sequence = _next_sequence(timesync_sequence)
        writer.write(build_mid_local_request(
            username=config.source_username,
            sequence=identity_sequence,
        ))
        await asyncio.wait_for(writer.drain(), timeout=config.timeout_seconds)
        identity_response = await asyncio.wait_for(
            reader.readuntil(b"</message>"),
            timeout=config.timeout_seconds,
        )
        identity_parameters = parse_mid_local_response(
            identity_response, expected_sequence=identity_sequence
        )
        context = _login_context(base_parameters, identity_parameters, config)
        session = QmfLoginSession(
            reader=reader,
            writer=writer,
            context=context,
            started_at=time.monotonic(),
            config=config,
        )
        return session
    except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
        raise QmfPreviewError("login_unavailable", "全民防登录服务暂时不可用") from exc
    except asyncio.LimitOverrunError as exc:
        raise QmfPreviewError("login_response_too_large", "全民防登录响应超过安全限制") from exc
    finally:
        if writer is not None and session is None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def login_readonly(config: QmfRuntimeConfig | None = None) -> QmfLoginContext:
    """Compatibility helper for callers that only need the identity context."""
    session = await open_login_session(config)
    try:
        return session.context
    finally:
        await session.close()


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
        login_provider: Callable[[], Any] | None = None,
        config: QmfRuntimeConfig | None = None,
    ):
        self._transport = transport
        self._login_provider = login_provider
        self._config = config or settings_config()

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
        url = urljoin(self._config.api_base_url.rstrip("/") + "/", endpoint)
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
        login_session: QmfLoginSession | None = None
        if self._login_provider is None:
            login_session = await open_login_session(self._config)
            login_context = login_session.context
        else:
            login_context = await self._login_provider()
        if not isinstance(login_context, QmfLoginContext):
            if login_session is not None:
                await login_session.close()
            raise QmfPreviewError("login_response_invalid", "全民防登录身份信息无效")

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        query_data = {
            "pcsbm": login_context.station_code,
            "sfzh": identity,
            "xm": "",
            "dz": "",
            # APK uses WpaUserData.userID, which is the input login account;
            # MID_LOCAL.MJJH is a separately refreshed display/work field.
            "mjjh": login_context.username,
            "hcjg": "",
            "cljg": "0",
            "pageSize": "10",
            "startTime": (now.date() - timedelta(days=92)).strftime("%Y%m%d"),
            "lxfs": "",
            "endTime": now.strftime("%Y%m%d"),
            "pageNum": "1",
            "source": "android",
        }
        timeout = httpx.Timeout(self._config.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
                headers={"User-Agent": "Binhu-QMF-Readonly/0.20.7"},
            ) as client:
                if login_session is not None:
                    login_session.ensure_available()
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
                    upstream_task["police_station"], self._config.expected_station_name
                ):
                    raise QmfPreviewError("task_station_mismatch", "全民防任务不属于目标派出所", 403)

                if login_session is not None:
                    login_session.ensure_available()
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
                if login_session is not None:
                    login_session.ensure_available()
                photo_response = await self._request(
                    client,
                    "jzz/queryLocalPhoto",
                    params={"sfzh": identity, "timestamp": str(int(time.time() * 1000))},
                )
                photo = _photo_payload(photo_response)

                if login_session is not None:
                    login_session.ensure_available()
                check_response = await self._request(
                    client,
                    "enterHouse/checkCk",
                    data={"sfzh": identity, "source": "android"},
                )
                _business_payload(check_response, expected_object=False)
        finally:
            if login_session is not None:
                await login_session.close()

        warnings = ["本页面仅用于人工核对，不会向全民防提交任何数据。"]
        task_community_code = upstream_task["community_code"]
        person_community_code = person["community_code"]
        if (
            task_community_code
            and person_community_code
            and task_community_code != person_community_code
        ):
            warnings.append(
                "任务辖区编码与人员社区编码来自不同编码体系，仅供人工核对；"
                "系统已按登录机构、任务派出所、身份证号和姓名完成范围校验。"
            )
        elif not task_community_code or not person_community_code:
            warnings.append(
                "任务辖区编码或人员社区编码缺失，仅供人工核对；"
                "系统已按登录机构、任务派出所、身份证号和姓名完成范围校验。"
            )

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
                # Captured successful workflows use different code systems for
                # task xfsq and PersonnelInfo communityCode. Jurisdiction is
                # enforced by the authenticated login station and the task
                # police-station field instead of comparing unrelated codes.
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
            "warnings": warnings,
        }


async def run_guarded_preview(
    *,
    platform_task: dict[str, Any],
    client: QmfReadOnlyClient | None = None,
    config: QmfRuntimeConfig | None = None,
) -> dict[str, Any]:
    global _preview_active, _last_preview_started
    now = time.monotonic()
    runtime_config = config or settings_config()
    cooldown = max(1, int(runtime_config.preview_cooldown_seconds))
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
        return await (client or QmfReadOnlyClient(config=runtime_config)).preview(
            platform_task=platform_task
        )
    finally:
        _preview_active = False


def reset_preview_guard_for_tests() -> None:
    global _preview_active, _last_preview_started
    _preview_active = False
    _last_preview_started = 0.0
