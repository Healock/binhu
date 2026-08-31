from services.address_matching import (
    ElasticsearchMatcher,
    MGeoReranker,
    match_address,
    normalize_address_text,
    parse_address,
)
from services.property_small_community_matching import match_property


ENTRIES = [
    {
        "id": 1,
        "name": "芦风华庭",
        "detail_address": "松陵芦荡路1288号芦风华庭11幢1502室",
        "aliases": ["芦风花庭", "芦风华园"],
        "community_id": 10,
        "community_name": "长板社区",
        "enabled": True,
    },
    {
        "id": 2,
        "name": "滨湖雅园",
        "detail_address": "松陵街道东太湖大道1000号",
        "aliases": ["雅园"],
        "community_id": 20,
        "community_name": "横扇社区",
        "enabled": True,
    },
]


def test_normalization_does_not_require_a_fixed_address_template():
    assert normalize_address_text(" 芦风华庭（１１栋）1502室 ") == "芦风华庭11幢1502室"
    parsed = parse_address("芦风华庭11栋1502室")
    assert parsed["road"] == ""
    assert parsed["house_number"] == ""
    assert parsed["building"] == "11幢"
    assert parsed["room"] == "1502室"


def test_complete_address_generates_suggestion_without_overwriting_original():
    original = "松陵芦荡路1288号芦风华庭11栋1502室"
    result = match_address(original, ENTRIES, community_name="长板社区")
    assert result["status"] == "suggested"
    assert result["candidate"]["entry_id"] == 1
    assert "小区名" in result["method"]
    assert original == "松陵芦荡路1288号芦风华庭11栋1502室"


def test_alias_and_incomplete_address_remain_reviewable_candidates():
    result = match_address("芦风花庭11号楼", ENTRIES, community_name="长板社区")
    assert result["candidate"]["entry_id"] == 1
    assert result["status"] in {"suggested", "ambiguous"}
    assert "小区别名" in result["method"]


def test_low_information_empty_and_non_residential_addresses_are_rejected():
    assert match_address("", ENTRIES)["status"] == "unmatched"
    assert match_address("20260828大圈_滨湖新城派出所", ENTRIES)["status"] == "invalid"
    assert match_address("某商场旁边", ENTRIES)["status"] == "invalid"


def test_community_and_street_conflicts_are_not_suggested():
    community_conflict = match_address(
        "芦风华庭11幢",
        ENTRIES,
        community_name="横扇社区",
    )
    assert community_conflict["status"] == "conflict"

    street_entries = [{
        **ENTRIES[1],
        "street": "松陵街道",
    }]
    street_conflict = match_address(
        "八坼街道东太湖大道1000号滨湖雅园",
        street_entries,
        street_name="八坼街道",
    )
    assert street_conflict["status"] == "conflict"


def test_same_name_in_multiple_communities_and_close_scores_require_review():
    duplicate = [
        {**ENTRIES[0], "id": 3},
        {**ENTRIES[0], "id": 4, "community_id": 11, "community_name": "联杨社区"},
    ]
    result = match_address("芦风华庭11幢", duplicate)
    assert result["status"] == "conflict"
    assert "多个社区" in result["reason"]

    ambiguous = match_address("雅园附近东太湖大道", ENTRIES)
    assert ambiguous["status"] in {"ambiguous", "invalid"}


def test_disabled_entries_are_never_candidates():
    result = match_address("芦风华庭", [{**ENTRIES[0], "enabled": False}])
    assert result["status"] == "unmatched"
    assert result["candidates"] == []


def test_property_matching_uses_current_history_and_alias_addresses():
    property_row = {
        "id": 88,
        "street": "松陵",
        "community_name": "长板社区",
        "natural_address": "无法直接识别的自然地址",
        "normalized_address": "",
        "building": "",
        "room": "",
        "history_addresses": ["芦荡路1288号芦风华庭"],
        "aliases": ["芦风花庭11幢"],
    }
    result = match_property(property_row, ENTRIES)
    assert result["status"] in {"suggested", "ambiguous"}
    assert result["candidate"]["entry_id"] == 1


def test_future_matcher_adapters_are_explicitly_disabled():
    try:
        ElasticsearchMatcher().match("地址", ENTRIES)
        assert False, "Elasticsearch matcher must stay disabled in the first release"
    except RuntimeError:
        pass
    try:
        MGeoReranker().rerank("地址", ENTRIES)
        assert False, "MGeo reranker must stay disabled in the first release"
    except RuntimeError:
        pass
