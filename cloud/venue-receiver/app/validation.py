"""Validation and conservative normalization for public venue submissions.

The browser performs quick feedback, but this module is the authoritative
boundary before a submission is encrypted and queued.  Normalization only
removes accidental whitespace and Unicode presentation differences; it never
guesses or rewrites a person's name or address.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from math import hypot
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ValidationError(ValueError):
    """A user-correctable validation error for one public field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_IDENTITY_RE = re.compile(r"^[0-9]{17}[0-9X]$")
# Keep the public contract readable as ^1[3-9]\d{9}$ while making \d ASCII-only.
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$", re.ASCII)
_IDENTITY_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_IDENTITY_CHECKS = "10X98765432"


def _normalize_text(value: str, *, field: str, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(field, f"{label}格式无效")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if _CONTROL_RE.search(normalized):
        raise ValidationError(field, f"{label}不能包含控制字符")
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValidationError(field, f"{label}不能为空")
    if len(normalized) > max_length:
        raise ValidationError(field, f"{label}长度不能超过{max_length}个字符")
    return normalized


def normalize_person_name(value: str) -> str:
    return _normalize_text(value, field="name", label="姓名", max_length=100)


def normalize_address(value: str) -> str:
    return _normalize_text(value, field="address", label="地址", max_length=500)


def validate_identity_number(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("identity_number", "公民身份号码格式无效")
    identity = unicodedata.normalize("NFKC", value).replace(" ", "").strip().upper()
    if not _IDENTITY_RE.fullmatch(identity):
        raise ValidationError("identity_number", "公民身份号码格式无效")
    try:
        date(int(identity[6:10]), int(identity[10:12]), int(identity[12:14]))
    except ValueError as exc:
        raise ValidationError("identity_number", "公民身份号码出生日期无效") from exc
    checksum = sum(int(digit) * weight for digit, weight in zip(identity[:17], _IDENTITY_WEIGHTS)) % 11
    if identity[-1] != _IDENTITY_CHECKS[checksum]:
        raise ValidationError("identity_number", "公民身份号码校验码无效")
    return identity


def validate_phone_number(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("phone", "手机号格式无效")
    # Only tolerate ordinary ASCII separators; the digit portion must match
    # the public contract exactly and must not be widened by Unicode folding.
    phone = value.strip().replace(" ", "").replace("-", "")
    if not _PHONE_RE.fullmatch(phone):
        raise ValidationError("phone", "手机号格式无效")
    return phone


def validate_public_submission_fields(
    *, name: str, identity_number: str, phone: str, address: str
) -> Mapping[str, str]:
    """Validate and normalize the four textual fields before encryption."""

    return {
        "name": normalize_person_name(name),
        "identity_number": validate_identity_number(identity_number),
        "phone": validate_phone_number(phone),
        "address": normalize_address(address),
    }


_DRINKING_LIMITS = {
    "name": ("姓名", 100),
    "unit_position": ("单位职务", 200),
    "drinking_place": ("饮酒地点", 500),
    "reason": ("饮酒事由", 500),
    "inviter": ("邀约人", 100),
    "travel_method": ("出行方式", 100),
    "responsible_leader_name": ("责任领导姓名", 100),
}


def validate_drinking_report_fields(payload: Mapping[str, Any], *, timezone_name: str) -> dict[str, str]:
    """Normalize a drinking report and convert its local wall time to UTC."""

    result = {
        field: _normalize_text(str(payload.get(field) or ""), field=field, label=label, max_length=maximum)
        for field, (label, maximum) in _DRINKING_LIMITS.items()
    }
    notes = str(payload.get("notes") or "")
    notes = unicodedata.normalize("NFKC", notes).strip()
    if _CONTROL_RE.search(notes):
        raise ValidationError("notes", "备注说明不能包含控制字符")
    notes = " ".join(notes.split())
    if len(notes) > 2000:
        raise ValidationError("notes", "备注说明长度不能超过2000个字符")
    raw_time = unicodedata.normalize("NFKC", str(payload.get("drinking_at") or "")).strip()
    try:
        local_time = datetime.fromisoformat(raw_time)
        if local_time.tzinfo is not None:
            aware_time = local_time
        else:
            try:
                aware_time = local_time.replace(tzinfo=ZoneInfo(timezone_name))
            except ZoneInfoNotFoundError as exc:
                raise ValidationError("drinking_at", "系统时区配置无效") from exc
        utc_time = aware_time.astimezone(timezone.utc)
    except ValueError as exc:
        raise ValidationError("drinking_at", "饮酒时间格式无效") from exc
    now = datetime.now(timezone.utc)
    if utc_time < now - timedelta(days=366) or utc_time > now + timedelta(days=366):
        raise ValidationError("drinking_at", "饮酒时间超出允许范围")
    result["drinking_at"] = utc_time.isoformat().replace("+00:00", "Z")
    result["notes"] = notes
    return result


def validate_signature_strokes(value: Any, *, field: str, label: str) -> list[list[dict[str, float]]]:
    """Validate normalized signature coordinates and reject taps or tiny marks."""

    if not isinstance(value, list) or not value or len(value) > 200:
        raise ValidationError(field, f"{label}无效")
    total_points = 0
    total_distance = 0.0
    normalized: list[list[dict[str, float]]] = []
    for stroke in value:
        if not isinstance(stroke, list) or not stroke or len(stroke) > 1000:
            raise ValidationError(field, f"{label}笔画无效")
        points: list[dict[str, float]] = []
        previous: tuple[float, float] | None = None
        for point in stroke:
            if not isinstance(point, dict):
                raise ValidationError(field, f"{label}坐标无效")
            try:
                x, y = float(point["x"]), float(point["y"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(field, f"{label}坐标无效") from exc
            if not 0 <= x <= 1 or not 0 <= y <= 1:
                raise ValidationError(field, f"{label}坐标无效")
            if previous is not None:
                total_distance += hypot(x - previous[0], y - previous[1])
            previous = (x, y)
            points.append({"x": round(x, 4), "y": round(y, 4)})
        total_points += len(points)
        normalized.append(points)
    if total_points > 5000:
        raise ValidationError(field, f"{label}点数过多")
    if total_distance < 0.02:
        raise ValidationError(field, f"请完整书写{label}")
    return normalized
