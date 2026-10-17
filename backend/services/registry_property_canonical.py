"""Validated, read-only canonical identity graph. No mapping creation API."""
from dataclasses import dataclass
import re

from fastapi import HTTPException


MAX_HOPS = 16


class PropertyIdentityError(ValueError):
    """Safe reason code only; never include business content in errors."""


@dataclass(frozen=True)
class PropertyResolution:
    requested_id: int
    canonical_id: int
    mapping_version: int = 0
    path: tuple[int, ...] = ()

    @property
    def redirected(self):
        return self.requested_id != self.canonical_id

    def payload(self):
        return {"requested_id": self.requested_id, "canonical_id": self.canonical_id,
                "redirected": self.redirected,
                "mapping_version": self.mapping_version, "path": list(self.path)}


class PropertyIdentityGraph:
    def __init__(self, edges=(), properties=()):
        self.edges = {}
        self.communities = {
            int(row[0]): (int(row[1]) if row[1] is not None else None)
            for row in properties
        }
        self.addresses = {
            int(row[0]): str(row[2] or "")
            for row in properties if len(row) > 2
        }
        for row in edges:
            if len(row) < 4:
                raise PropertyIdentityError("invalid_mapping_row")
            old, target, community, version = row[:4]
            old, target = int(old), int(target)
            if old in self.edges:
                raise PropertyIdentityError("duplicate_active_target")
            if old <= 0 or target <= 0 or int(version) <= 0:
                raise PropertyIdentityError("invalid_mapping")
            if old == target:
                raise PropertyIdentityError("self_mapping")
            if community is None or int(community) <= 0:
                raise PropertyIdentityError("community_missing")
            self.edges[old] = (target, int(community), int(version))
        # Validate the entire graph, not just IDs on the visible page.
        self.resolutions = {old: self._resolve(old) for old in self.edges}
        if any(target in self.edges for target, _, _ in self.edges.values()):
            raise PropertyIdentityError("canonical_target_is_historical")

    def _resolve(self, requested):
        node, seen, version, path = requested, set(), 0, []
        for _ in range(MAX_HOPS + 1):
            if node in seen:
                raise PropertyIdentityError("mapping_cycle")
            seen.add(node)
            path.append(node)
            edge = self.edges.get(node)
            if edge is None:
                return PropertyResolution(requested, node, version, tuple(path))
            if len(seen) > MAX_HOPS:
                raise PropertyIdentityError("mapping_depth_exceeded")
            target, community, revision = edge
            if node not in self.communities or target not in self.communities:
                raise PropertyIdentityError("mapping_endpoint_missing")
            if self.communities[node] != community or self.communities[target] != community:
                raise PropertyIdentityError("cross_community_mapping")
            old_address = self.addresses.get(node, "")
            current_address = self.addresses.get(target, "")
            if not old_address or not current_address or old_address != current_address:
                raise PropertyIdentityError("cross_address_mapping")
            version = max(version, revision)
            node = target
        raise PropertyIdentityError("mapping_depth_exceeded")

    def resolve(self, property_id):
        identifier = int(property_id)
        if identifier <= 0:
            raise PropertyIdentityError("invalid_property_id")
        return self.resolutions.get(identifier, PropertyResolution(identifier, identifier, 0, (identifier,)))

    def resolve_many(self, property_ids):
        return {int(value): self.resolve(value) for value in property_ids}

    def family(self, property_ids):
        targets = {self.resolve(value).canonical_id for value in property_ids}
        return targets | {old for old, result in self.resolutions.items() if result.canonical_id in targets}

    def require_current(self, property_ids):
        if any(self.resolve(value).canonical_id != int(value) for value in property_ids):
            raise HTTPException(409, {"code": "stale_property_identity",
                                     "message": "房屋身份已更新，请刷新后重新选择现行房屋再重试"})

    def current_predicate(self, alias="property"):
        # Alias is internal code, never request input. A validated graph is
        # required before this exclusion is used for COUNT/order/pagination.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(alias)):
            raise ValueError("invalid_property_table_alias")
        return (
            f"NOT EXISTS (SELECT 1 FROM registry_property_identity_mappings identity_map "
            f"WHERE identity_map.old_property_id={alias}.id AND identity_map.status='active')",
            (),
        )


async def load_property_identity_graph(cur, *, schema=None):
    prefix = f"`{schema.replace('`', '``')}`." if schema else ""
    await cur.execute(
        "SELECT old_property_id,canonical_property_id,community_id,version "
        f"FROM {prefix}registry_property_identity_mappings WHERE status='active' ORDER BY old_property_id"
    )
    edges = list(await cur.fetchall())
    properties = []
    ids = sorted({int(value) for row in edges for value in row[:2]})
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset + 500]
        await cur.execute(f"SELECT id,community_id,normalized_address FROM {prefix}registry_properties "
                          f"WHERE id IN ({','.join(['%s'] * len(batch))})", tuple(batch))
        for row in await cur.fetchall():
            if len(row) >= 3:
                properties.append((row[0], row[1], row[2]))
    try:
        return PropertyIdentityGraph(edges, properties)
    except PropertyIdentityError as exc:
        raise HTTPException(409, {"code": "property_identity_invalid", "reason": str(exc),
                                 "message": "房屋归并关系异常，请联系档案管理员核查后重试"}) from None


async def require_current_property_ids(cur, property_ids, *, schema=None):
    graph = await load_property_identity_graph(cur, schema=schema)
    graph.require_current(property_ids)
    return graph
