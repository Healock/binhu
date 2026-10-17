import json
from unittest.mock import AsyncMock

import pytest

from services.registry_issue_reconciliation import (
    community_labels,
    issue_matches_community,
    organization_pool_key,
    unique_community_matches,
)


def test_current_alias_matches_stored_unresolved_issue():
    labels = community_labels("江城", ["南刘村", "江城社区"])
    payload = json.dumps({"community": "南刘村", "address": "胜信路178号"})
    assert issue_matches_community(payload, labels)


def test_reconciliation_does_not_guess_unrelated_or_malformed_issue():
    labels = community_labels("江城", ["南刘村"])
    assert not issue_matches_community({"community": "祥泰社区"}, labels)
    assert not issue_matches_community("not-json", labels)
    assert not issue_matches_community({"address": "胜信路178号"}, labels)


def test_labels_are_exact_and_normalized():
    assert community_labels(" 江城 ", [" 南刘村 ", ""]) == {"江城", "南刘村"}


def test_unique_community_match_requires_one_active_owner():
    payload = {"community": "南刘村"}
    labels = {1: ("江城", {"江城", "南刘村"}), 2: ("祥泰", {"祥泰"})}
    assert unique_community_matches(payload, labels) == (1, "江城")

    ambiguous = {
        1: ("江城", {"江城", "南刘村"}),
        2: ("祥泰", {"祥泰", "南刘村"}),
    }
    assert unique_community_matches(payload, ambiguous) is None


def test_reconciliation_uses_the_active_organization_domain():
    assert organization_pool_key(False) == "online_data"
    assert organization_pool_key(True) == "platform"


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [False, True])
async def test_reconciliation_uses_the_same_registration_lineage_rules(historical):
    from services.registry_issue_reconciliation import _reconcile_source_row
    from tests.test_household_multi_file_import import IdentityConnection, existing_property, row
    conn = IdentityConnection(sources=[], properties=[existing_property(
        number="NEW" if historical else "OLD", updated_at="2026-01-01" if historical else "2020-01-01",
        status="未注销" if historical else "已注销",
    )])
    conn.rowcount = 1
    conn.fetchone = AsyncMock(return_value=None)
    payload = row("synthetic", house_no="OLD" if historical else "NEW",
                  updated_at="2020-01-01" if historical else "2026-01-01",
                  household_status="已注销" if historical else "未注销")
    assert await _reconcile_source_row(conn, batch_id=10, source_ref="synthetic", payload=payload,
                                       community_id=8, community_name="合成社区", reviewer_id=7)
    assert not any(sql.startswith("INSERT INTO registry_properties") for sql, _ in conn.calls)
    assert any(sql.startswith("UPDATE registry_properties") for sql, _ in conn.calls) is not historical
