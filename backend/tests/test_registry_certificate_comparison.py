import os

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_certificate_comparison import (
    _canonicalize_comparison_inputs,
    compare_certificate_snapshot,
    load_certificate_comparison,
)
from services.registry_certificate_source import certificate_content_hash, certificate_source_ref
from services.registry_property_canonical import PropertyIdentityGraph


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


def test_source_identifier_change_reuses_the_single_notice_for_the_house():
    old = _row(ref="notice-1")
    new = _row(ref="notice-2")
    result = compare_certificate_snapshot(
        [new], [_existing(old)],
        [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["unchanged"] == 1
    assert result["added"] == 0
    assert result["updated"] == 0
    assert result["missing_from_source"] == 0


def test_source_identifier_and_content_change_is_one_update_for_the_house():
    old = _row(ref="notice-1", renter="甲")
    new = _row(ref="notice-2", renter="乙")
    result = compare_certificate_snapshot(
        [new], [_existing(old)],
        [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["updated"] == 1
    assert result["added"] == 0
    assert result["missing_from_source"] == 0


def test_multiple_existing_notices_for_one_house_are_reviewed_not_selected():
    first = _existing(_row(ref="notice-1"))
    second = _existing(_row(ref="notice-2"))
    result = compare_certificate_snapshot(
        [_row(ref="notice-3")], [first, second],
        [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["pending_review"] == 1
    assert result["safe_to_apply"] == 0
    assert result["issue_breakdown"]["duplicate"] == 1
    assert result["status_summary"]["multiple_or_conflict"]["total"] == 1


def test_multiple_incoming_notices_for_one_house_are_all_reviewed():
    result = compare_certificate_snapshot(
        [_row(ref="notice-1"), _row(ref="notice-2")], [],
        [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["pending_review"] == 2
    assert result["safe_to_apply"] == 0
    assert result["issue_breakdown"]["duplicate"] == 1
    assert result["status_summary"]["multiple_or_conflict"]["total"] == 1


def test_source_identifier_owned_by_another_house_is_reviewed_not_added():
    old = _row(address="合成甲路1号")
    new = _row(address="合成乙路2号")
    result = compare_certificate_snapshot(
        [new], [_existing(old)],
        [{"id": 7, "normalized_address": "合成甲路1号", "community_name": "长板社区", "housing_type": "个人出租"},
         {"id": 8, "normalized_address": "合成乙路2号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["pending_review"] == 1
    assert result["safe_to_apply"] == 0


def test_same_incoming_source_identifier_cannot_be_applied_to_two_houses():
    result = compare_certificate_snapshot(
        [_row(address="合成甲路1号"), _row(address="合成乙路2号")], [],
        [{"id": 7, "normalized_address": "合成甲路1号", "community_name": "长板社区", "housing_type": "个人出租"},
         {"id": 8, "normalized_address": "合成乙路2号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["pending_review"] == 2
    assert result["safe_to_apply"] == 0


def test_unmatched_and_non_rental_records_are_pending_review():
    result = compare_certificate_snapshot(
        [_row(address="不存在路99号"), _row(ref="notice-2", address="自购房1号")],
        [],
        [{"id": 8, "normalized_address": "自购房1号", "community_name": "长板社区", "housing_type": "自购房屋"}],
    )
    assert result["pending_review"] == 2
    assert result["issue_breakdown"]["property_not_found"] == 1
    assert result["issue_breakdown"]["non_rental_property"] == 1


def test_local_notice_missing_from_complete_source_no_longer_counts_as_current():
    old = _row()
    result = compare_certificate_snapshot(
        [], [_existing(old)], [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["missing_from_source"] == 1
    assert result["status_summary"]["not_required"]["total"] == 1
    assert result["status_summary"]["normal_signed"]["total"] == 0


def test_status_totals_include_existing_rental_properties_without_a_notice():
    result = compare_certificate_snapshot(
        [], [], [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
    )
    assert result["status_summary"]["not_required"]["total"] == 1


def test_status_total_counts_unchanged_house_once_and_conflict_is_not_unsigned():
    row = _row()
    properties = [{"id": 7, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}]
    result = compare_certificate_snapshot([row], [_existing(row)], properties)
    assert result["status_summary"]["normal_signed"]["total"] == 1
    result = compare_certificate_snapshot([], [_existing(row)], properties,
                                          conflict_keys={("湖滨路1号", "长板社区")})
    assert result["status_summary"]["multiple_or_conflict"]["total"] == 1
    assert result["status_summary"]["not_required"]["total"] == 0


def test_historical_and_current_certificates_project_to_current_property_as_conflict():
    row = _row()
    graph = PropertyIdentityGraph(
        edges=[(10, 20, 3, 1)],
        properties=[(10, 3, "湖滨路1号"), (20, 3, "湖滨路1号")],
    )
    properties, existing = _canonicalize_comparison_inputs(
        [
            {"id": 10, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"},
            {"id": 20, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"},
        ],
        [_existing(row, property_id=10), _existing(row, property_id=20)],
        graph,
    )

    result = compare_certificate_snapshot([row], existing, properties)

    assert [item["id"] for item in properties] == [20]
    assert [item["property_id"] for item in existing] == [20, 20]
    assert result["pending_review"] == 1
    assert result["safe_to_apply"] == 0
    assert result["status_summary"]["multiple_or_conflict"]["total"] == 1


def test_historical_and_current_duplicate_notices_remain_visible_after_projection():
    graph = PropertyIdentityGraph(
        edges=[(10, 20, 3, 1)],
        properties=[(10, 3, "湖滨路1号"), (20, 3, "湖滨路1号")],
    )
    first = _row(ref="notice-1")
    second = _row(ref="notice-2")
    properties, existing = _canonicalize_comparison_inputs(
        [{"id": 20, "normalized_address": "湖滨路1号", "community_name": "长板社区", "housing_type": "个人出租"}],
        [_existing(first, property_id=10), _existing(second, property_id=20)], graph,
    )
    result = compare_certificate_snapshot([_row(ref="notice-3")], existing, properties)
    assert result["pending_review"] == 1
    assert result["safe_to_apply"] == 0
    assert result["status_summary"]["multiple_or_conflict"]["total"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_comparison_loader_resolves_existing_certificate_before_counting(duplicate):
    import json

    class Cursor:
        def __init__(self):
            self.rows = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, sql, _params=()):
            if sql.startswith("SELECT old_property_id,canonical_property_id,community_id,version"):
                self.rows = [(10, 20, 3, 1)]
            elif sql.startswith("SELECT id,community_id,normalized_address FROM registry_properties"):
                self.rows = [(10, 3, "湖滨路1号"), (20, 3, "湖滨路1号")]
            elif "LEFT JOIN OnlineData._communities" in sql:
                self.rows = [
                    (10, "湖滨路1号", "个人出租", "长板社区"),
                    (20, "湖滨路1号", "个人出租", "长板社区"),
                ]
            elif "JOIN OnlineData._community_aliases" in sql:
                self.rows = []
            elif sql.startswith("SELECT property_id,source_ref,source_content_hash"):
                old = _row()
                self.rows = [(
                    10, old["source_ref"], old["source_content_hash"], old["sjczrxm"],
                    "", old["czrxm"], old["isSign"], old["signType"], json.dumps(old),
                )]
                if duplicate:
                    self.rows.append(self.rows[0])
            else:
                self.rows = []

        async def fetchall(self):
            return self.rows

    class Connection:
        def cursor(self):
            return Cursor()

    incoming = _row()
    result = await load_certificate_comparison(
        Connection(), [incoming], {"normal_rows": [incoming], "problem_row_count": 0},
    )

    assert result["added"] == 0
    if duplicate:
        assert result["pending_review"] == 1
        assert result["safe_to_apply"] == 0
        assert result["issue_breakdown"]["duplicate"] == 1
        assert result["status_summary"]["multiple_or_conflict"]["total"] == 1
    else:
        assert result["unchanged"] == 1
        assert result["status_summary"]["normal_signed"]["total"] == 1
