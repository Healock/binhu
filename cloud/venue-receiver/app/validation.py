"""Validation and conservative normalization for public venue submissions.

The browser performs quick feedback, but this module is the authoritative
boundary before a submission is encrypted and queued.  Normalization only
removes accidental whitespace and Unicode presentation differences; it never
guesses or rewrites a person's name or address.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Mapping


class ValidationError(ValueError):
    """A user-correctable validation error for one public field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_IDENTITY_RE = re.compile(r"^[0-9]{17}[0-9X]$")
_PHONE_RE = re.compile(r"^1[3-9][0-9]{9}$")
_IDENTITY_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_IDENTITY_CHECKS = "10X98765432"


def _normalize_text(value: str, *, field: str, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(field, f"{label}格式无效")
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValidationError(field, f"{label}不能为空")
    if len(normalized) > max_length:
        raise ValidationError(field, f"{label}长度不能超过{max_length}个字符")
    if _CONTROL_RE.search(normalized):
        raise ValidationError(field, f"{label}不能包含控制字符")
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
    phone = unicodedata.normalize("NFKC", value).replace(" ", "").replace("-", "").strip()
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
