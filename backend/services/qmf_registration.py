"""全民防模型三单条预演与封闭登记客户端。

只读与写入路径分别使用精确白名单。真实登记仅实现已由单条成功样本
确认的四个写接口和固定顺序，不提供任意路径、任意正文或通用转发能力。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx

from config import settings  # kept as a compatibility patch target for tests
from services.qmf_community import (
    normalize_qmf_community_code,
    valid_qmf_community_code,
)
from services.qmf_config import QmfRuntimeConfig, settings_config


MODEL_THREE_PARSER = "疑似未注销模型三"
RESULT_IN_WU = "在吴"
RESULT_RECENT_RETURN = "近期返吴"
RESULT_LEAVE_NOT_RETURNING = "离开不返吴"
RESULT_ALIASES = {
    RESULT_IN_WU: RESULT_IN_WU,
    RESULT_RECENT_RETURN: RESULT_RECENT_RETURN,
    "近期反吴": RESULT_RECENT_RETURN,
    "离吴": RESULT_LEAVE_NOT_RETURNING,
    RESULT_LEAVE_NOT_RETURNING: RESULT_LEAVE_NOT_RETURNING,
}
SUPPORTED_RESULT = RESULT_IN_WU  # compatibility for older imports/tests
READ_ONLY_ENDPOINTS = {
    "fnmx/queryYysList": "POST",
    "enterHouse/queryPeopleBySfzh": "POST",
    "enterHouse/queryPeoplePhotoByJzz": "POST",
    "enterHouse/checkCk": "POST",
    "declare/queryCommunityCode": "POST",
}
READ_ONLY_ENDPOINT_CONTEXT = {
    "fnmx/queryYysList": ("query_task", "任务查询"),
    "enterHouse/queryPeopleBySfzh": ("query_person", "人员资料查询"),
    "enterHouse/queryPeoplePhotoByJzz": ("query_photo", "居住证照片查询"),
    "enterHouse/checkCk": ("precheck", "登记前校验"),
    "declare/queryCommunityCode": ("query_community", "社区代码核对"),
}
READ_ONLY_MAX_ATTEMPTS = 2
READ_ONLY_RETRY_DELAY_SECONDS = 0.35
READ_ONLY_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})
WRITE_ENDPOINTS = {
    "masses/uploadPhoto": "POST",
    "jzz/saveLocalPhoto": "POST",
    "enterHouse/addPeople": "POST",
    "fnmx/fnmxCheck": "POST",
}
PERSONNEL_INFO_FIELDS = frozenset({
    "alias", "beizhu", "birth", "cbqk", "communityCode", "cylx",
    "degree", "djrq", "dwjlx", "dwjlxdz", "dwlxdh", "dz", "emsdh",
    "emsdz", "emsxm", "fby", "fwbh", "fwhh", "gender", "gxr1",
    "gxr2", "gxr3", "gxr4", "gxrjzk1", "gxrjzk2", "gxrjzk3",
    "gxrjzk4", "gxrq1", "gxrq2", "gxrq3", "gxrq4", "gxsfz1",
    "gxsfz2", "gxsfz3", "gxsfz4", "gxsj", "gxxb1", "gxxb2",
    "gxxb3", "gxxb4", "gxxm1", "gxxm2", "gxxm3", "gxxm4",
    "height", "hjdz", "hjdzxz", "hunyin", "id", "isems", "jqjzym",
    "jzfs", "jzlx", "jzsy", "lsrq", "name", "namepy", "nation",
    "njzsj", "personID", "phone", "politicalStaus", "qjbh", "sbsbh",
    "sfbljzz", "sfcb", "sflz", "sfzx", "sltj", "slyy", "szdw",
    "wllxfs", "yfzgx", "zns",
})
PERSONNEL_QUERY_ONLY_FIELDS = frozenset({"alias", "qjbh", "wllxfs"})
ADD_PEOPLE_FIELDS = frozenset(
    (PERSONNEL_INFO_FIELDS - PERSONNEL_QUERY_ONLY_FIELDS)
    | {
        "canEdit", "csrq", "email", "fromPersonCheck", "isPhotoFromJzz",
        "mArrImgs", "operateBy", "operateType", "qq", "wx",
    }
)
MAX_LOGIN_RESPONSE_BYTES = 256 * 1024
MAX_PHOTO_BYTES = 5 * 1024 * 1024
_IDENTITY_PATTERN = re.compile(r"^\d{17}[0-9X]$")
_IDENTITY_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_IDENTITY_CHECKS = "10X98765432"
_GENDER_LABELS = {
    "0": "未知",
    "1": "男",
    "2": "女",
    "9": "未说明",
}
_NATION_LABELS = {
    "01": "汉族", "02": "蒙古族", "03": "回族", "04": "藏族",
    "05": "维吾尔族", "06": "苗族", "07": "彝族", "08": "壮族",
    "09": "布依族", "10": "朝鲜族", "11": "满族", "12": "侗族",
    "13": "瑶族", "14": "白族", "15": "土家族", "16": "哈尼族",
    "17": "哈萨克族", "18": "傣族", "19": "黎族", "20": "傈僳族",
    "21": "佤族", "22": "畲族", "23": "高山族", "24": "拉祜族",
    "25": "水族", "26": "东乡族", "27": "纳西族", "28": "景颇族",
    "29": "柯尔克孜族", "30": "土族", "31": "达斡尔族", "32": "仫佬族",
    "33": "羌族", "34": "布朗族", "35": "撒拉族", "36": "毛南族",
    "37": "仡佬族", "38": "锡伯族", "39": "阿昌族", "40": "普米族",
    "41": "塔吉克族", "42": "怒族", "43": "乌孜别克族", "44": "俄罗斯族",
    "45": "鄂温克族", "46": "德昂族", "47": "保安族", "48": "裕固族",
    "49": "京族", "50": "塔塔尔族", "51": "独龙族", "52": "鄂伦春族",
    "53": "赫哲族", "54": "门巴族", "55": "珞巴族", "56": "基诺族",
    "97": "其他", "98": "外国血统", "99": "未说明",
}
_EDUCATION_LABELS = {
    "10": "研究生",
    "11": "博士研究生",
    "12": "硕士研究生",
    "19": "研究生班",
    "20": "大学本科",
    "30": "大学专科",
    "40": "中等专科",
    "50": "技工学校",
    "60": "高中",
    "70": "初中",
    "80": "小学",
    "90": "文盲或半文盲",
    "99": "未说明",
}
_MARITAL_STATUS_LABELS = {
    "1": "未婚", "2": "已婚", "3": "丧偶", "4": "离婚", "9": "未说明",
    "10": "未婚", "20": "已婚", "21": "初婚", "22": "再婚", "23": "复婚",
    "30": "丧偶", "40": "离婚", "90": "未说明",
}


class QmfPreviewError(RuntimeError):
    """A safe, non-sensitive error suitable for an API response and audit."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 502,
        *,
        uncertain: bool = False,
        step: str = "",
        upstream_status: int | None = None,
        transport_error: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.uncertain = uncertain
        self.step = step
        self.upstream_status = upstream_status
        self.transport_error = transport_error


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
_registration_active = False


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


def normalize_qmf_result(value: Any) -> str:
    return RESULT_ALIASES.get(_text(value), "")


def preview_capability(
    *,
    allowed: bool,
    parser_type: str,
    source_count: int,
    conflict: bool,
    values: dict[str, Any] | None,
    config: QmfRuntimeConfig | None = None,
) -> dict[str, Any]:
    """Return server-computed visibility without exposing configuration values."""
    visible = allowed and parser_type == MODEL_THREE_PARSER
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
    if not normalize_qmf_result(values.get("核查结果")):
        return {
            "visible": True,
            "enabled": False,
            "reason": "当前仅支持“在吴、近期返吴、离开不返吴”三种结果",
        }
    if not valid_identity(values.get("身份证号")):
        return {
            "visible": True,
            "enabled": False,
            "reason": "任务身份证号格式无效，不能预演",
        }
    return {"visible": True, "enabled": True, "reason": ""}


def registration_capability(
    *,
    allowed: bool,
    parser_type: str,
    source_count: int,
    conflict: bool,
    values: dict[str, Any] | None,
    config: QmfRuntimeConfig | None = None,
) -> dict[str, Any]:
    runtime_config = config or settings_config()
    preview = preview_capability(
        allowed=allowed,
        parser_type=parser_type,
        source_count=source_count,
        conflict=conflict,
        values=values,
        config=runtime_config,
    )
    if not preview["visible"]:
        return preview
    if not preview["enabled"]:
        return preview
    if not runtime_config.registration_configured:
        return {
            "visible": True,
            "enabled": False,
            "reason": "全民防登记尚未开启或接口配置不完整",
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


def _transport_error_kind(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, httpx.ReadTimeout)):
        return "read_timeout"
    if isinstance(exc, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, (ConnectionError, ConnectionRefusedError)):
        return "connect_error"
    if isinstance(exc, asyncio.IncompleteReadError):
        return "incomplete_read"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "connection_error"
    if isinstance(exc, httpx.RequestError):
        return "request_error"
    if isinstance(exc, OSError):
        return "connection_error"
    return "request_error"


def _readonly_unavailable_message(transport_error: str) -> str:
    labels = {
        "read_timeout": "响应超时",
        "write_timeout": "发送超时",
        "connect_timeout": "连接超时",
        "connect_error": "连接失败",
        "connection_error": "连接失败",
        "incomplete_read": "响应中断",
        "request_error": "请求失败",
    }
    label = labels.get(transport_error, "请求失败")
    return f"全民防只读接口{label}，已自动重试一次仍不可用"


async def _open_login_session_once(config: QmfRuntimeConfig) -> QmfLoginSession:
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
        raise QmfPreviewError(
            "login_unavailable",
            "全民防登录服务暂时不可用",
            step="login",
            transport_error=_transport_error_kind(exc),
        ) from exc
    except asyncio.LimitOverrunError as exc:
        raise QmfPreviewError("login_response_too_large", "全民防登录响应超过安全限制") from exc
    finally:
        if writer is not None and session is None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def open_login_session(config: QmfRuntimeConfig | None = None) -> QmfLoginSession:
    config = config or settings_config()
    try:
        return await _open_login_session_once(config)
    except QmfPreviewError as exc:
        if exc.code != "login_unavailable":
            raise
        await asyncio.sleep(READ_ONLY_RETRY_DELAY_SECONDS)
        try:
            return await _open_login_session_once(config)
        except QmfPreviewError as retry_exc:
            if retry_exc.code != "login_unavailable":
                raise
            raise QmfPreviewError(
                "login_unavailable",
                "全民防登录服务暂时不可用，已自动重试一次仍不可用",
                retry_exc.status_code,
                step="login",
                transport_error=retry_exc.transport_error or exc.transport_error,
            ) from retry_exc


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
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json":
        if not response.content or len(response.content) > MAX_PHOTO_BYTES * 2:
            raise QmfPreviewError(
                "photo_size_invalid", "居住证照片为空或超过安全大小限制"
            )
        data = _business_payload(response, expected_object=False)
        if not isinstance(data, str):
            raise QmfPreviewError("photo_response_invalid", "居住证照片响应结构无效")
        compact = re.sub(r"\s+", "", data)
        max_encoded_size = ((MAX_PHOTO_BYTES + 2) // 3) * 4
        if not compact or len(compact) > max_encoded_size:
            raise QmfPreviewError(
                "photo_size_invalid", "居住证照片为空或超过安全大小限制"
            )
        try:
            content = base64.b64decode(compact, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise QmfPreviewError(
                "photo_base64_invalid", "居住证照片编码校验失败"
            ) from exc
        content_type = ""
    else:
        content = response.content
    if not content or len(content) > MAX_PHOTO_BYTES:
        raise QmfPreviewError("photo_size_invalid", "居住证照片为空或超过安全大小限制")
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


def _display_code(value: Any, labels: dict[str, str]) -> str:
    text = _text(value)
    if not text:
        return ""
    if text in labels:
        return labels[text]
    return f"代码 {text}（待确认）" if text.isdigit() else text


def _display_birth_date(value: Any, identity: str) -> tuple[str, bool]:
    text = _text(value)
    compact = re.sub(r"[^0-9]", "", text)
    if len(compact) == 8:
        try:
            parsed = datetime.strptime(compact, "%Y%m%d")
        except ValueError:
            pass
        else:
            return parsed.strftime("%Y-%m-%d"), False
    if text:
        return text, False
    if valid_identity(identity):
        parsed = datetime.strptime(identity[6:14], "%Y%m%d")
        return parsed.strftime("%Y-%m-%d"), True
    return "", False


def _safe_person(person: dict[str, Any]) -> dict[str, Any]:
    """Project only fields required for the authenticated human comparison."""
    identity = normalize_identity(_first(person, "personID", "sfzh"))
    gender_code = _first(person, "gender", "xb")
    nation_code = _first(person, "nation", "mz")
    education_code = _first(person, "degree", "whcd")
    marital_status_code = _first(person, "hunyin", "hyzk")
    birth_date, birth_date_derived = _display_birth_date(
        _first(person, "birth", "csrq"), identity
    )
    return {
        "name": _first(person, "name", "xm"),
        "identity_number": identity,
        "phone": _first(person, "phone", "lxfs", "wllxfs"),
        "current_address": _first(person, "dz", "address"),
        "household_address": _first(person, "hjdzxz", "hjdz"),
        "gender": _display_code(gender_code, _GENDER_LABELS),
        "gender_code": gender_code,
        "birth_date": birth_date,
        "birth_date_derived": birth_date_derived,
        "nation": _display_code(nation_code, _NATION_LABELS),
        "nation_code": nation_code,
        "education": _display_code(education_code, _EDUCATION_LABELS),
        "education_code": education_code,
        "marital_status": _display_code(
            marital_status_code, _MARITAL_STATUS_LABELS
        ),
        "marital_status_code": marital_status_code,
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


@dataclass(frozen=True)
class QmfCollectedContext:
    platform_task: dict[str, Any]
    login_context: QmfLoginContext
    query_data: dict[str, str]
    raw_task: dict[str, Any]
    upstream_task: dict[str, str]
    raw_person: dict[str, Any] | None
    person: dict[str, Any] | None
    photo: dict[str, Any] | None


StepCallback = Callable[[str, str, str], Awaitable[None]]
BeforeWriteCallback = Callable[[QmfCollectedContext], Awaitable[None]]


def _query_data(
    *,
    login_context: QmfLoginContext,
    identity: str,
    now: datetime | None = None,
) -> dict[str, str]:
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return {
        "pcsbm": login_context.station_code,
        "sfzh": identity,
        "xm": "",
        "dz": "",
        # APK uses the input account (WpaUserData.userID), not MID_LOCAL.MJJH.
        "mjjh": login_context.username,
        "hcjg": "",
        "cljg": "0",
        "pageSize": "10",
        "startTime": (current.date() - timedelta(days=92)).strftime("%Y%m%d"),
        "lxfs": "",
        "endTime": current.strftime("%Y%m%d"),
        "pageNum": "1",
        "source": "android",
    }


def _matching_pending_tasks(
    task_data: Any,
    *,
    identity: str,
) -> tuple[int, list[dict[str, Any]]]:
    if not isinstance(task_data, dict):
        raise QmfPreviewError("task_response_invalid", "全民防任务响应结构无效")
    raw_tasks = task_data.get("list")
    if not isinstance(raw_tasks, list):
        raise QmfPreviewError("task_response_invalid", "全民防任务响应结构无效")
    try:
        total_tasks = int(task_data.get("total", -1))
    except (TypeError, ValueError) as exc:
        raise QmfPreviewError(
            "task_response_invalid", "全民防任务响应结构无效"
        ) from exc
    matching = [
        item for item in raw_tasks
        if isinstance(item, dict)
        and normalize_identity(_first(item, "sfzh", "personID")) == identity
    ]
    return total_tasks, matching


def _registration_photo_path(
    *,
    login_username: str,
    task_community_code: str,
    photo_sha256: str,
) -> str:
    # The successful APK sample constructs mArrImgs from the login account,
    # task xfsq and the local-photo SHA-256.  It is metadata only; the image
    # bytes have already been uploaded by masses/uploadPhoto.
    components = (login_username, task_community_code, photo_sha256)
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]+", login_username)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", task_community_code)
        or not re.fullmatch(r"[0-9a-f]{64}", photo_sha256)
    ):
        raise QmfPreviewError(
            "photo_reference_invalid", "全民防照片引用参数无效", 409
        )
    return (
        "/storage/emulated/0/.Wpa_Android_Base_WJ_Wgldpt/"
        f"{components[0]}/{components[1]}/Ry/Temp/{components[2]}.0"
    )


def build_upload_photo_payload(context: QmfCollectedContext) -> dict[str, Any]:
    if not context.person or not context.photo:
        raise QmfPreviewError("person_write_fields_missing", "全民防人员登记资料缺失", 409)
    identity = normalize_identity(context.person.get("identity_number"))
    community_code = _text(context.person.get("community_code"))
    gender = _text(context.person.get("gender_code"))
    name = _text(context.person.get("name"))
    if not all((valid_identity(identity), community_code, gender, name)):
        raise QmfPreviewError(
            "person_write_fields_missing", "全民防人员登记资料缺少写入必需字段", 409
        )
    return {
        "csrq": identity[6:14],
        "jmzh": identity,
        "sfbljzz": "1",
        "sqdm": community_code,
        "txsj": str(context.photo.get("data_base64") or ""),
        "xb": gender,
        "xm": name,
    }


def build_add_people_payload(
    context: QmfCollectedContext,
    *,
    now: datetime | None = None,
    device_id: str,
) -> dict[str, Any]:
    raw_person = context.raw_person
    if raw_person is None:
        raise QmfPreviewError("person_write_fields_missing", "全民防人员登记资料缺失", 409)
    if set(raw_person) != PERSONNEL_INFO_FIELDS:
        raise QmfPreviewError(
            "person_schema_changed", "全民防人员资料字段结构已变化，已停止登记", 409
        )
    payload: dict[str, Any] = {}
    for key in PERSONNEL_INFO_FIELDS - PERSONNEL_QUERY_ONLY_FIELDS:
        value = raw_person[key]
        if isinstance(value, (dict, list, tuple, set)):
            raise QmfPreviewError(
                "person_schema_changed", "全民防人员资料字段结构已变化，已停止登记", 409
            )
        payload[key] = "" if value is None else value

    identity = normalize_identity(payload.get("personID"))
    if not valid_identity(identity):
        raise QmfPreviewError("identity_invalid", "全民防人员身份证号格式无效", 409)
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    payload.update({
        "beizhu": context.login_context.operator_name,
        "canEdit": False,
        "csrq": identity[6:14],
        "djrq": current.strftime("%Y%m%d"),
        "email": "",
        "fromPersonCheck": True,
        "isPhotoFromJzz": False,
        "mArrImgs": [_registration_photo_path(
            login_username=context.login_context.username,
            task_community_code=context.upstream_task["community_code"],
            photo_sha256=str(context.photo.get("sha256") or ""),
        )],
        "operateBy": context.login_context.username,
        "operateType": "2",
        "qq": "",
        "sbsbh": device_id,
        "sfbljzz": "1",
        "wx": "",
    })
    if set(payload) != ADD_PEOPLE_FIELDS:
        raise QmfPreviewError(
            "person_payload_invalid", "全民防人员登记字段构造失败", 500
        )
    return payload


def build_fnmx_check_payload(
    context: QmfCollectedContext,
    *,
    device_id: str,
) -> dict[str, str]:
    result = normalize_qmf_result(context.platform_task.get("result"))
    if result == RESULT_LEAVE_NOT_RETURNING:
        community_code = normalize_qmf_community_code(
            context.platform_task.get("qmf_community_code")
        )
        destination_code = _text(context.platform_task.get("destination_code"))
        destination_address = _text(context.platform_task.get("destination_address"))
        if (
            not valid_qmf_community_code(community_code)
            or not re.fullmatch(r"\d{6}", destination_code)
            or not destination_address
        ):
            raise QmfPreviewError(
                "leave_fields_missing", "离开不返吴所需社区或去往地信息不完整", 409
            )
        return {
            "xfid": context.upstream_task["task_id"],
            "logoutReason": "2",
            "hcjg": "1",
            "hcczr": context.login_context.username,
            "sbsbh": device_id,
            "personID": normalize_identity(context.platform_task["identity_number"]),
            "type": "3",
            "qwdxzqh": destination_code,
            "communityCode": community_code,
            "qwdxz": destination_address,
            "source": "android",
        }
    if not context.person:
        raise QmfPreviewError("person_write_fields_missing", "全民防人员登记资料缺失", 409)
    return {
        "communityCode": "",
        "hcczr": context.login_context.username,
        "hcjg": "3" if result == RESULT_RECENT_RETURN else "2",
        "logoutReason": "",
        "personID": normalize_identity(context.person["identity_number"]),
        "qwdxz": "",
        "qwdxzqh": "",
        "sbsbh": device_id,
        "source": "android",
        "type": "3",
        "xfid": context.upstream_task["task_id"],
    }


def _write_business_payload(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise QmfPreviewError(
            "write_response_invalid",
            "全民防写入接口响应结构无效，结果需要人工复核",
            uncertain=True,
        ) from exc
    if not isinstance(payload, dict):
        raise QmfPreviewError(
            "write_response_invalid",
            "全民防写入接口响应结构无效，结果需要人工复核",
            uncertain=True,
        )
    if str(payload.get("code", "")) != "200":
        raise QmfPreviewError("write_business_error", "全民防写入接口返回业务错误")
    return payload.get("data")


def _precheck_payload(response: httpx.Response) -> None:
    """Validate the exact successful checkCk contract from the verified sample."""
    data = _business_payload(response, expected_object=False)
    if type(data) is not int or data != 0:
        raise QmfPreviewError(
            "precheck_rejected",
            "全民防登记前校验未通过，已停止登记",
            409,
        )


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
        step, label = READ_ONLY_ENDPOINT_CONTEXT.get(
            endpoint, ("readonly_request", "只读")
        )
        for attempt in range(READ_ONLY_MAX_ATTEMPTS):
            try:
                response = await client.request(method, url, data=data, params=params)
            except httpx.RequestError as exc:
                if attempt + 1 < READ_ONLY_MAX_ATTEMPTS:
                    await asyncio.sleep(READ_ONLY_RETRY_DELAY_SECONDS)
                    continue
                transport_error = _transport_error_kind(exc)
                raise QmfPreviewError(
                    "upstream_unavailable",
                    _readonly_unavailable_message(transport_error),
                    step=step,
                    transport_error=transport_error,
                ) from exc
            if not response.is_success:
                if (
                    response.status_code in READ_ONLY_RETRYABLE_HTTP_STATUS
                    and attempt + 1 < READ_ONLY_MAX_ATTEMPTS
                ):
                    await response.aclose()
                    await asyncio.sleep(READ_ONLY_RETRY_DELAY_SECONDS)
                    continue
                retry_note = (
                    "，已自动重试一次仍不可用"
                    if response.status_code in READ_ONLY_RETRYABLE_HTTP_STATUS
                    else ""
                )
                raise QmfPreviewError(
                    "upstream_http_error",
                    f"全民防{label}接口返回 HTTP {response.status_code}{retry_note}",
                    step=step,
                    upstream_status=response.status_code,
                )
            return response
        raise QmfPreviewError(
            "upstream_unavailable",
            "全民防只读接口暂时不可用",
            step=step,
        )

    async def _emit_step(
        self,
        callback: StepCallback | None,
        key: str,
        status: str,
        result_code: str = "",
    ) -> None:
        if callback is not None:
            await callback(key, status, result_code)

    async def _collect_context(
        self,
        *,
        client: httpx.AsyncClient,
        login_session: QmfLoginSession | None,
        login_context: QmfLoginContext,
        platform_task: dict[str, Any],
        step_callback: StepCallback | None = None,
    ) -> QmfCollectedContext:
        identity = normalize_identity(platform_task.get("identity_number"))
        name = _text(platform_task.get("name"))
        if not valid_identity(identity):
            raise QmfPreviewError("identity_invalid", "任务身份证号格式无效", 422)
        query_data = _query_data(login_context=login_context, identity=identity)

        result = normalize_qmf_result(platform_task.get("result"))
        if not result:
            raise QmfPreviewError("result_not_supported", "当前核查结果不支持全民防登记", 422)

        if login_session is not None:
            login_session.ensure_available()
        await self._emit_step(step_callback, "query_task", "sending")
        task_response = await self._request(
            client, "fnmx/queryYysList", data=query_data
        )
        task_data = _business_payload(task_response, expected_object=True)
        total_tasks, matching_tasks = _matching_pending_tasks(
            task_data, identity=identity
        )
        if total_tasks != 1 or len(matching_tasks) != 1:
            code = "task_not_found" if not matching_tasks else "task_not_unique"
            raise QmfPreviewError(code, "全民防未找到唯一待处理任务", 409)
        raw_task = matching_tasks[0]
        upstream_task = _safe_upstream_task(raw_task)
        if not upstream_task["task_id"] or not upstream_task["record_id"]:
            raise QmfPreviewError("task_identity_missing", "全民防任务标识不完整")
        if (
            upstream_task["identity_number"] != identity
            or _normalized_name(upstream_task["name"]) != _normalized_name(name)
        ):
            raise QmfPreviewError(
                "task_person_mismatch", "全民防任务人员与平台任务不一致", 409
            )
        if not _station_matches(
            upstream_task["police_station"], self._config.expected_station_name
        ):
            raise QmfPreviewError(
                "task_station_mismatch", "全民防任务不属于目标派出所", 403
            )
        await self._emit_step(step_callback, "query_task", "succeeded", "success")

        if result == RESULT_LEAVE_NOT_RETURNING:
            if login_session is not None:
                login_session.ensure_available()
            await self._emit_step(step_callback, "query_community", "sending")
            community_response = await self._request(
                client,
                "declare/queryCommunityCode",
                data={"sfzh": identity, "source": "android"},
            )
            _business_payload(community_response, expected_object=False)
            await self._emit_step(
                step_callback, "query_community", "succeeded", "success"
            )
            return QmfCollectedContext(
                platform_task=platform_task,
                login_context=login_context,
                query_data=query_data,
                raw_task=raw_task,
                upstream_task=upstream_task,
                raw_person=None,
                person=None,
                photo=None,
            )

        if login_session is not None:
            login_session.ensure_available()
        await self._emit_step(step_callback, "query_person", "sending")
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
            raise QmfPreviewError(
                "person_mismatch", "全民防人员登记资料与平台任务不一致", 409
            )
        await self._emit_step(step_callback, "query_person", "succeeded", "success")

        photo_person_id = normalize_identity(_first(raw_person, "personID"))
        if not photo_person_id or photo_person_id != identity:
            raise QmfPreviewError(
                "photo_person_mismatch",
                "全民防居住证照片人员标识与平台任务不一致",
                409,
            )

        if login_session is not None:
            login_session.ensure_available()
        await self._emit_step(step_callback, "query_photo", "sending")
        photo_response = await self._request(
            client,
            "enterHouse/queryPeoplePhotoByJzz",
            data={"personID": photo_person_id, "source": "android"},
        )
        photo = _photo_payload(photo_response)
        await self._emit_step(step_callback, "query_photo", "succeeded", "success")

        if login_session is not None:
            login_session.ensure_available()
        await self._emit_step(step_callback, "precheck", "sending")
        check_response = await self._request(
            client,
            "enterHouse/checkCk",
            data={"sfzh": identity, "source": "android"},
        )
        _precheck_payload(check_response)
        await self._emit_step(step_callback, "precheck", "succeeded", "success")
        return QmfCollectedContext(
            platform_task=platform_task,
            login_context=login_context,
            query_data=query_data,
            raw_task=raw_task,
            upstream_task=upstream_task,
            raw_person=raw_person,
            person=person,
            photo=photo,
        )

    @staticmethod
    def _preview_payload(context: QmfCollectedContext) -> dict[str, Any]:
        result = normalize_qmf_result(context.platform_task.get("result"))
        if result == RESULT_LEAVE_NOT_RETURNING:
            return {
                "mode": "read_only",
                "can_submit": False,
                "platform_task": context.platform_task,
                "upstream_task": context.upstream_task,
                "person": None,
                "operator": {
                    "username": context.login_context.username,
                    "name": context.login_context.operator_name,
                    "station_code": context.login_context.station_code,
                    "station_name": context.login_context.station_name,
                },
                "photo": None,
                "destination": {
                    "community": context.platform_task.get("resolved_community", ""),
                    "community_code": context.platform_task.get("qmf_community_code", ""),
                    "area_code": context.platform_task.get("destination_code", ""),
                    "area_name": context.platform_task.get("destination_address", ""),
                },
                "checks": {
                    "source_revision": True,
                    "single_source": True,
                    "identity_match": True,
                    "name_match": True,
                    "single_upstream_task": True,
                    "station_match": True,
                    "community_code_valid": True,
                    "destination_valid": True,
                },
                "planned_write_steps": [
                    {"key": "complete_task", "label": "反馈模型三注销结果", "enabled": False},
                ],
                "planned_changes": [{
                    "key": "complete_task",
                    "label": "模型三反馈",
                    "detail": "反馈为“离开不返吴”，社区取平台社区代码，去往行政区划和地址取身份证前六位对应户籍地区",
                }],
                "warnings": ["本结果不登记人员、不读取或上传照片；确认后只执行一次模型三注销反馈。"],
            }
        warnings = ["本页面仅用于人工核对；只有完成二次确认后才会登记。"]
        assert context.person is not None and context.photo is not None
        task_community_code = context.upstream_task["community_code"]
        person_community_code = context.person["community_code"]
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
            "platform_task": context.platform_task,
            "upstream_task": context.upstream_task,
            "person": context.person,
            "operator": {
                "username": context.login_context.username,
                "name": context.login_context.operator_name,
                "station_code": context.login_context.station_code,
                "station_name": context.login_context.station_name,
            },
            "photo": context.photo,
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
            "planned_changes": [
                {
                    "key": "upload_photo",
                    "label": "照片数据",
                    "detail": "上传本次校验通过的居住证照片",
                },
                {
                    "key": "save_local_photo",
                    "label": "照片关联",
                    "detail": "按已核验合同保存居住证照片关联",
                },
                {
                    "key": "register_person",
                    "label": "人员登记",
                    "detail": (
                        "基于完整 PersonnelInfo 保留原业务字段，并更新登记日期、"
                        "操作人、设备标识、登记标记和备注"
                    ),
                },
                {
                    "key": "complete_task",
                    "label": "模型三反馈",
                    "detail": (
                        f"固定反馈为“{result}”；communityCode、logoutReason、"
                        "qwdxzqh、qwdxz 保持空字符串"
                    ),
                },
            ],
            "warnings": warnings,
        }

    async def preview(
        self,
        *,
        platform_task: dict[str, Any],
    ) -> dict[str, Any]:
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
        timeout = httpx.Timeout(self._config.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
                headers={"User-Agent": "Binhu-QMF-Readonly/0.21.2"},
            ) as client:
                context = await self._collect_context(
                    client=client,
                    login_session=login_session,
                    login_context=login_context,
                    platform_task=platform_task,
                )
        finally:
            if login_session is not None:
                await login_session.close()
        return self._preview_payload(context)


class QmfRegistrationClient(QmfReadOnlyClient):
    async def _write_request(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        data: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
        files: dict[str, tuple[None, str]] | None = None,
    ) -> httpx.Response:
        method = WRITE_ENDPOINTS.get(endpoint)
        if not method:
            raise QmfPreviewError(
                "write_endpoint_not_allowed", "请求不在全民防写入白名单", 500
            )
        url = urljoin(self._config.api_base_url.rstrip("/") + "/", endpoint)
        try:
            response = await client.request(
                method, url, data=data, json=json_payload, files=files
            )
        except httpx.RequestError as exc:
            raise QmfPreviewError(
                "write_result_uncertain",
                "全民防写入请求结果无法确认，已停止后续步骤",
                uncertain=True,
            ) from exc
        if not response.is_success:
            raise QmfPreviewError(
                "write_http_error",
                "全民防写入接口返回 HTTP 错误，结果需要人工复核",
                uncertain=True,
            )
        return response

    async def execute(
        self,
        *,
        platform_task: dict[str, Any],
        step_callback: StepCallback,
        before_write: BeforeWriteCallback,
    ) -> dict[str, Any]:
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

        timeout = httpx.Timeout(self._config.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
                headers={"User-Agent": "Binhu-QMF-Registration/0.21.2"},
            ) as client:
                context = await self._collect_context(
                    client=client,
                    login_session=login_session,
                    login_context=login_context,
                    platform_task=platform_task,
                    step_callback=step_callback,
                )
                fnmx_payload = build_fnmx_check_payload(
                    context,
                    device_id=self._config.source_imei,
                )
                result = normalize_qmf_result(context.platform_task.get("result"))
                if result != RESULT_LEAVE_NOT_RETURNING:
                    # Construct and validate every write body before the first write.
                    upload_payload = build_upload_photo_payload(context)
                    add_people_payload = build_add_people_payload(
                        context,
                        device_id=self._config.source_imei,
                    )
                await before_write(context)

                if result != RESULT_LEAVE_NOT_RETURNING:
                    assert context.person is not None
                    if login_session is not None:
                        login_session.ensure_available()
                    await self._emit_step(step_callback, "upload_photo", "sending")
                    response = await self._write_request(
                        client,
                        "masses/uploadPhoto",
                        json_payload=upload_payload,
                    )
                    _write_business_payload(response)
                    await self._emit_step(
                        step_callback, "upload_photo", "succeeded", "success"
                    )

                    if login_session is not None:
                        login_session.ensure_available()
                    await self._emit_step(step_callback, "save_local_photo", "sending")
                    response = await self._write_request(
                        client,
                        "jzz/saveLocalPhoto",
                        files={
                            "idCard": (None, context.person["identity_number"]),
                            "imageType": (None, "3"),
                            "label": (None, "2"),
                            "createBy": (None, context.login_context.username),
                        },
                    )
                    _write_business_payload(response)
                    await self._emit_step(
                        step_callback, "save_local_photo", "succeeded", "success"
                    )

                    if login_session is not None:
                        login_session.ensure_available()
                    await self._emit_step(step_callback, "register_person", "sending")
                    response = await self._write_request(
                        client,
                        "enterHouse/addPeople",
                        json_payload=add_people_payload,
                    )
                    _write_business_payload(response)
                    await self._emit_step(
                        step_callback, "register_person", "succeeded", "success"
                    )

                if login_session is not None:
                    login_session.ensure_available()
                await self._emit_step(step_callback, "complete_task", "sending")
                response = await self._write_request(
                    client,
                    "fnmx/fnmxCheck",
                    data=fnmx_payload,
                )
                _write_business_payload(response)
                await self._emit_step(
                    step_callback, "complete_task", "succeeded", "success"
                )

                if login_session is not None:
                    login_session.ensure_available()
                await self._emit_step(step_callback, "verify_final", "sending")
                try:
                    final_response = await self._request(
                        client, "fnmx/queryYysList", data=context.query_data
                    )
                    final_data = _business_payload(
                        final_response, expected_object=True
                    )
                except QmfPreviewError as exc:
                    raise QmfPreviewError(
                        "final_state_unconfirmed",
                        "全民防最终状态尚未确认，禁止重复登记",
                        409,
                        uncertain=True,
                    ) from exc
                total, matching = _matching_pending_tasks(
                    final_data,
                    identity=normalize_identity(
                        context.platform_task["identity_number"]
                    ),
                )
                if total != 0 or matching:
                    raise QmfPreviewError(
                        "final_state_unconfirmed",
                        "全民防最终状态尚未确认，禁止重复登记",
                        409,
                        uncertain=True,
                    )
                await self._emit_step(
                    step_callback, "verify_final", "succeeded", "success"
                )
                return {
                    "status": "succeeded",
                    "upstream_task_id": context.upstream_task["task_id"],
                    "photo": ({
                        "mime_type": context.photo["mime_type"],
                        "size_bytes": context.photo["size_bytes"],
                        "sha256": context.photo["sha256"],
                    } if context.photo else {}),
                }
        finally:
            if login_session is not None:
                await login_session.close()


async def run_guarded_preview(
    *,
    platform_task: dict[str, Any],
    client: QmfReadOnlyClient | None = None,
    config: QmfRuntimeConfig | None = None,
) -> dict[str, Any]:
    global _preview_active
    runtime_config = config or settings_config()
    if _preview_active or _registration_active:
        raise QmfPreviewError("preview_busy", "已有一条全民防预演正在执行", 429)
    _preview_active = True
    try:
        return await (client or QmfReadOnlyClient(config=runtime_config)).preview(
            platform_task=platform_task
        )
    finally:
        _preview_active = False


async def run_guarded_registration(
    *,
    platform_task: dict[str, Any],
    step_callback: StepCallback,
    before_write: BeforeWriteCallback,
    client: QmfRegistrationClient | None = None,
    config: QmfRuntimeConfig | None = None,
) -> dict[str, Any]:
    global _registration_active
    if _preview_active or _registration_active:
        raise QmfPreviewError("registration_busy", "已有一条全民防任务正在执行", 429)
    _registration_active = True
    try:
        runtime_config = config or settings_config()
        return await (client or QmfRegistrationClient(config=runtime_config)).execute(
            platform_task=platform_task,
            step_callback=step_callback,
            before_write=before_write,
        )
    finally:
        _registration_active = False


def qmf_operation_busy() -> bool:
    return _preview_active or _registration_active


def reset_preview_guard_for_tests() -> None:
    global _preview_active, _registration_active
    _preview_active = False
    _registration_active = False
