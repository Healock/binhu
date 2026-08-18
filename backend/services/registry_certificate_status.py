"""Responsibility-notice status shown in the housing archive."""

from __future__ import annotations

from typing import Any


RENTAL_HOUSING_TYPES = {"个人出租", "单位出租"}

CERTIFICATE_STATUS_LABELS = {
    "normal_signed": "正常签署",
    "not_required": "无需上传告知书",
    "not_uploaded": "未上传告知书",
    "renter_needs_correction": "已签署，需修改实际出租人",
    "actual_renter_missing": "实际出租人未确定",
    "multiple_or_conflict": "告知书来源待核对",
    "not_applicable": "非出租房屋",
}


def _text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u3000", " ").split())


def _is_signed(value: Any) -> bool:
    return _text(value).lower() in {"是", "已签署", "已签", "true", "1", "yes"}


def certificate_status_summary(
    *,
    housing_type: Any,
    certificate_count: int = 0,
    certificate_issue_count: int = 0,
    source_ready: bool = True,
    landlord_name: Any = "",
    actual_renter_name: Any = "",
    signed_status: Any = "",
    sign_type: Any = "",
    updated_at: Any = None,
    responsibility_identity: str = "",
) -> dict[str, Any]:
    housing = _text(housing_type)
    landlord = _text(landlord_name)
    renter = _text(actual_renter_name)
    count = int(certificate_count or 0)
    issue_count = int(certificate_issue_count or 0)

    if housing not in RENTAL_HOUSING_TYPES:
        status = "not_applicable"
    elif count > 1 or issue_count > 0:
        status = "multiple_or_conflict"
    elif count == 0:
        status = "not_required" if source_ready else "not_uploaded"
    elif not renter:
        status = "actual_renter_missing"
    elif _is_signed(signed_status):
        status = "normal_signed"
    elif _text(sign_type):
        status = "renter_needs_correction"
    else:
        status = "not_uploaded"

    if status == "not_required":
        relation = "not_required"
        relation_label = "平台无该房屋记录，无需上传"
    elif status == "multiple_or_conflict" and count == 0:
        relation = "conflict"
        relation_label = "来源记录存在问题，需先核对"
    elif not renter:
        relation = "unknown"
        relation_label = "实际出租人未确定"
    elif landlord and landlord == renter:
        relation = "same"
        relation_label = "房东与实际出租人一致"
    elif landlord:
        relation = "different"
        relation_label = "房东与实际出租人不一致"
    else:
        relation = "unknown"
        relation_label = "房东身份未确定"

    return {
        "certificate_status": status,
        "certificate_status_label": CERTIFICATE_STATUS_LABELS[status],
        "certificate_count": count,
        "certificate_issue_count": issue_count,
        "certificate_source_ready": bool(source_ready),
        "certificate_updated_at": updated_at,
        "landlord_renter_relation": relation,
        "landlord_renter_relation_label": relation_label,
        "actual_renter_status": "confirmed" if renter else "unknown",
        "responsibility_identity": responsibility_identity or "未确认",
    }
