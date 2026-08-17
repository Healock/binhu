from __future__ import annotations

import os

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_certificate_source import (
    certificate_content_hash,
    certificate_source_ref,
)
from services.registry_certificate_apply import certificate_write_action
from services.registry_certificate_status import certificate_status_summary


def test_source_reference_is_stable_when_physical_row_changes():
    first = {"dztzm": "notice-001", "source_row": 3, "signType": "线下"}
    moved = {"dztzm": "notice-001", "source_row": 937, "signType": "线下"}

    assert certificate_source_ref(first) == certificate_source_ref(moved)


def test_content_hash_ignores_source_metadata_but_detects_business_change():
    original = {
        "dztzm": "notice-001",
        "source_row": 3,
        "source_ref": "old-ref",
        "isSign": "否",
    }
    moved = {**original, "source_row": 300, "source_ref": "new-ref"}
    signed = {**moved, "isSign": "是"}

    assert certificate_content_hash(original) == certificate_content_hash(moved)
    assert certificate_content_hash(original) != certificate_content_hash(signed)


def test_workbook_compatible_notice_statuses_are_explicit():
    assert certificate_status_summary(
        housing_type="个人出租",
        certificate_count=1,
        landlord_name="甲",
        actual_renter_name="甲",
        signed_status="是",
    )["certificate_status"] == "normal_signed"

    assert certificate_status_summary(
        housing_type="个人出租",
        certificate_count=0,
    )["certificate_status"] == "not_uploaded"

    assert certificate_status_summary(
        housing_type="个人出租",
        certificate_count=1,
        landlord_name="甲",
        actual_renter_name="乙",
        signed_status="否",
        sign_type="已签署",
    )["certificate_status"] == "renter_needs_correction"

    missing = certificate_status_summary(
        housing_type="个人出租",
        certificate_count=1,
        landlord_name="甲",
        actual_renter_name="",
        signed_status="是",
    )
    assert missing["certificate_status"] == "actual_renter_missing"
    assert missing["landlord_renter_relation"] == "unknown"


def test_multiple_notices_override_the_nominal_signature_state():
    summary = certificate_status_summary(
        housing_type="单位出租",
        certificate_count=2,
        landlord_name="甲",
        actual_renter_name="甲",
        signed_status="是",
    )

    assert summary["certificate_status"] == "multiple_or_conflict"


def test_existing_notice_is_updated_when_content_or_legacy_reference_changes():
    existing = (7, "房东责任告知书只读接口:12", 9, "old-hash", {})

    assert certificate_write_action(
        existing,
        source_ref="certificate:notice:new",
        content_hash="old-hash",
        property_id=9,
    ) == "update"
    assert certificate_write_action(
        (7, "certificate:notice:new", 9, "old-hash", {}),
        source_ref="certificate:notice:new",
        content_hash="new-hash",
        property_id=9,
    ) == "update"
    assert certificate_write_action(
        (7, "certificate:notice:new", 9, "new-hash", {}),
        source_ref="certificate:notice:new",
        content_hash="new-hash",
        property_id=9,
    ) == "unchanged"
