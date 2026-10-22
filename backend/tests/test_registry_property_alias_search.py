from pathlib import Path


def test_property_keyword_search_includes_small_community_aliases():
    source = Path(__file__).parents[1].joinpath("routers", "registry.py").read_text(encoding="utf-8")
    assert "CAST(entry.aliases_json AS CHAR) LIKE %s" in source
    assert "entry.id=property_match.small_community_id" in source
    assert "alias_like" in source
    assert "alias_fragment" in source


def test_certificate_family_ranking_uses_the_defined_derived_table_alias():
    source = Path(__file__).parents[1].joinpath("routers", "registry.py").read_text(encoding="utf-8")
    assert '"SELECT ranked_sources.*,ROW_NUMBER() OVER (PARTITION BY canonical_property_id "' in source
    assert '"SELECT distinct_sources.*,ROW_NUMBER() OVER (PARTITION BY canonical_property_id "' not in source
