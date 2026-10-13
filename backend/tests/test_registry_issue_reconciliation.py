import json

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
