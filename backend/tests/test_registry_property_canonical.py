import pytest

from services.registry_property_canonical import PropertyIdentityError, PropertyIdentityGraph


def test_empty_graph_is_compatible_and_current_predicate_is_safe():
    graph = PropertyIdentityGraph(properties=[(10, 3)])
    result = graph.resolve(10)
    assert result.payload() == {
        "requested_id": 10,
        "canonical_id": 10,
        "redirected": False,
        "mapping_version": 0,
        "path": [10],
    }
    predicate, params = graph.current_predicate()
    assert predicate.startswith("NOT EXISTS")
    assert params == ()


def test_single_mapping_resolves_and_excludes_only_old_id():
    graph = PropertyIdentityGraph(
        edges=[(10, 20, 3, 4)],
        properties=[(10, 3, "甲路1号"), (20, 3, "甲路1号")],
    )
    assert graph.resolve(10).canonical_id == 20
    assert graph.resolve(10).path == (10, 20)
    assert graph.resolve(20).canonical_id == 20
    assert graph.family([20]) == {10, 20}


@pytest.mark.parametrize("edges,properties,reason", [
    ([(10, 10, 3, 1)], [(10, 3, "甲路1号")], "self_mapping"),
    ([(10, 20, 3, 1), (20, 10, 3, 1)],
     [(10, 3, "甲路1号"), (20, 3, "甲路1号")], "mapping_cycle"),
    ([(10, 20, 3, 1)], [(10, 3, "甲路1号"), (20, 4, "甲路1号")], "cross_community_mapping"),
    ([(10, 20, 3, 1)], [(10, 3, "甲路1号")], "mapping_endpoint_missing"),
    ([(10, 20, None, 1)], [(10, 3, "甲路1号"), (20, 3, "甲路1号")], "community_missing"),
    ([(10, 20, 3, 1)], [(10, 3, "甲路1号"), (20, 3, "甲路2号")], "cross_address_mapping"),
    ([(10, 20, 3, 1)], [(10, 3, ""), (20, 3, "甲路1号")], "cross_address_mapping"),
    ([(10, 20, 3, 1), (20, 30, 3, 1)],
     [(10, 3, "甲路1号"), (20, 3, "甲路1号"), (30, 3, "甲路1号")],
     "canonical_target_is_historical"),
])
def test_invalid_graphs_fail_closed(edges, properties, reason):
    with pytest.raises(PropertyIdentityError, match=reason):
        PropertyIdentityGraph(edges=edges, properties=properties)


def test_mapping_requires_nonempty_equal_canonical_addresses_and_terminal_target():
    with pytest.raises(PropertyIdentityError, match="cross_address_mapping"):
        PropertyIdentityGraph(edges=[(10, 20, 3, 1)], properties=[(10, 3), (20, 3)])

    graph = PropertyIdentityGraph(
        edges=[(10, 20, 3, 2)],
        properties=[(10, 3, "甲路1号"), (20, 3, "甲路1号")],
    )
    assert graph.resolve(10).canonical_id == 20
    assert graph.resolve(10).mapping_version == 2


def test_duplicate_active_old_id_fails_closed():
    with pytest.raises(PropertyIdentityError, match="duplicate_active_target"):
        PropertyIdentityGraph(
            edges=[(10, 20, 3, 1), (10, 30, 3, 2)],
            properties=[(10, 3, "甲路1号"), (20, 3, "甲路1号"), (30, 3, "甲路1号")],
        )


def test_deep_chain_fails_closed():
    edges = [(index, index + 1, 3, 1) for index in range(1, 18)]
    properties = [(index, 3, "甲路1号") for index in range(1, 19)]
    with pytest.raises(PropertyIdentityError, match="mapping_depth_exceeded"):
        PropertyIdentityGraph(edges=edges, properties=properties)
