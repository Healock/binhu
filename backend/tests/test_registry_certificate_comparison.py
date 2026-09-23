import os

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_certificate_comparison import compare_certificate_snapshot
from services.registry_certificate_source import certificate_content_hash, certificate_source_ref


def _row(ref="notice-1", *, signed="是", renter="甲", address="湖滨路1号"):
    row = {
        "dztzm": ref,
        "community": "长板社区",
        "address": address,
        "czrxm": "甲",
        "sjczrxm": renter,
        "isSign": signed,
        "signType": "",
        "signurl": "2026-01-01/a.jpg",
    }
    row["source_ref"] = certificate_source_ref(row)
    row["source_content_hash"] = certificate_content_hash(row)
    return row


def _existing(row, property_id=7):
    return {
        "property_id": property_id,
        "source_ref": row["source_ref"],
        "source_content_hash": row["source_content_hash"],
        "payload": row,
    }


def test_first_complete_snapshot_counts_safe_records_as_added():
    result = compare_certificate_snapshot(
        [_row()], [], [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["added"] == 1
    assert result["safe_to_apply"] == 1
    assert result["missing_from_source"] == 0


def test_same_snapshot_is_unchanged_and_row_position_is_not_business_change():
    row = _row()
    incoming = {**row, "source_row": 999}
    result = compare_certificate_snapshot(
        [incoming], [_existing(row)], [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["unchanged"] == 1
    assert result["updated"] == 0


def test_mutable_renter_change_is_updated_not_added_and_missing():
    old = _row(renter="甲")
    new = _row(renter="乙")
    result = compare_certificate_snapshot(
        [new], [_existing(old)], [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["updated"] == 1
    assert result["added"] == 0
    assert result["missing_from_source"] == 0


def test_unmatched_and_non_rental_records_are_pending_review():
    result = compare_certificate_snapshot(
        [_row(address="不存在路99号"), _row(ref="notice-2", address="自购房1号")],
        [],
        [{"id": 8, "normalized_address": "自购房1号", "community_name": "长板社区", "housing_type": "自购房屋"}],
    )
    assert result["pending_review"] == 2
    assert result["issue_breakdown"]["property_not_found"] == 1
    assert result["issue_breakdown"]["non_rental_property"] == 1


def test_local_notice_missing_from_complete_source_is_only_reported():
    old = _row()
    result = compare_certificate_snapshot(
        [], [_existing(old)], [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["missing_from_source"] == 1


def test_status_totals_include_existing_rental_properties_without_a_notice():
    result = compare_certificate_snapshot(
        [], [], [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["status_summary"]["not_required"]["total"] == 1
