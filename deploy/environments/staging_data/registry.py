"""Explicit field contract for the current address graph, not a database dump.

Unlisted JSON, attachments, source references and external identifiers are never
selected. Defaults below are newly generated Staging values, not source copies.
"""
from __future__ import annotations
from .codec import Codec, SnapshotError, enum, integer, date_value

FIELDS = {
    "PlatformData._areas": ("id", "name"),
    "PlatformData._communities": ("id", "name", "area_id", "is_active"),
    "RegistryData._police_address_entries": ("id", "name", "detail_address", "community_id", "address_type", "enabled"),
    "RegistryData.registry_properties": ("id", "street", "community_id", "natural_address", "building", "room", "housing_type", "residence_type", "source_house_no", "status", "current_version"),
    "RegistryData.registry_property_small_community_links": ("property_id", "small_community_id", "community_id", "match_status", "confirmed_by", "confirmed_at", "property_version"),
}
KINDS = {
    "PlatformData._areas": "area", "PlatformData._communities": "community",
    "RegistryData._police_address_entries": "small_community",
    "RegistryData.registry_properties": "property",
}


def validate_input(rows):
    if set(rows) != set(FIELDS):
        raise SnapshotError("registry_table_scope_mismatch")
    for table, items in rows.items():
        if any(set(row) != set(FIELDS[table]) for row in items):
            raise SnapshotError("registry_column_scope_mismatch")
        key = "property_id" if table.endswith("links") else "id"
        ids = [row[key] for row in items]
        if len(ids) != len(set(ids)):
            raise SnapshotError("duplicate_registry_primary_key")


def transform(rows, codec: Codec, *, actor_ids=(), exclude_orphan_property_links=False):
    from services.registry_import import NORMAL_HOUSING_TYPES
    validate_input(rows)
    for table, kind in KINDS.items():
        codec.allocate(kind, [row["id"] for row in rows[table]])
    actors = {row["confirmed_by"] for row in rows["RegistryData.registry_property_small_community_links"] if row["confirmed_by"] is not None} | set(actor_ids)
    codec.allocate("actor", actors)
    output = {name: [] for name in FIELDS}
    communities = {row["id"]: row for row in rows["PlatformData._communities"]}
    entries = {row["id"]: row for row in rows["RegistryData._police_address_entries"]}
    properties = {row["id"]: row for row in rows["RegistryData.registry_properties"]}
    rejected = []

    def community_name(value):
        if value is None:
            return ""
        codec.reference("community", value, nullable=False)
        codec.remember(communities[value]["name"])
        return "验证社区" + str(codec.reference("community", value))

    for row in rows["PlatformData._areas"]:
        output["PlatformData._areas"].append({"id": codec.reference("area", row["id"]), "name": codec.text("area_name", row["name"], "验证片区")})
    for row in communities.values():
        output["PlatformData._communities"].append({
            "id": codec.reference("community", row["id"]), "name": community_name(row["id"]),
            "area_id": codec.reference("area", row["area_id"]), "is_active": integer(row["is_active"], maximum=1),
            "police_officers": "[]", "qmf_community_code": None})
    for row in entries.values():
        output["RegistryData._police_address_entries"].append({
            "id": codec.reference("small_community", row["id"]),
            "name": codec.text("small_community_name", row["name"], "验证小区"),
            "normalized_name": codec.text("small_community_name", row["name"], "验证小区"),
            "detail_address": codec.address(row["community_id"], row["detail_address"]),
            "community_id": codec.reference("community", row["community_id"]),
            "address_type": enum(row["address_type"], {"community", "apartment", "construction_dormitory", "other"}, empty=False),
            "enabled": integer(row["enabled"], maximum=1), "aliases_json": "[]", "source_flags": "[]", "pattern": ""})
    for row in properties.values():
        address = codec.address(row["community_id"], row["natural_address"])
        output["RegistryData.registry_properties"].append({
            "id": codec.reference("property", row["id"]),
            "street": codec.text("street", row["street"], "验证街道"),
            "community_id": codec.reference("community", row["community_id"]),
            "community_name_snapshot": community_name(row["community_id"]),
            "natural_address": address, "normalized_address": address,
            "building": codec.text("building", row["building"], "楼"),
            "room": codec.text("room", row["room"], "室"),
            "housing_type": enum(row["housing_type"], NORMAL_HOUSING_TYPES),
            "residence_type": codec.text("residence_type", row["residence_type"], "用途"),
            "source_house_no": codec.text("house_no", row["source_house_no"], "STG"),
            "status": enum(row["status"], {"active", "inactive", "archived", "deleted"}, empty=False),
            "current_version": integer(row["current_version"], minimum=1, maximum=2**32-1),
            "source_type": "manual", "source_ref": ""})
    for row in rows["RegistryData.registry_property_small_community_links"]:
        prop = properties.get(row["property_id"])
        if prop is None:
            raise SnapshotError("orphan_property_link")
        entry = entries.get(row["small_community_id"])
        if row['small_community_id'] is not None and entry is None:
            if (not exclude_orphan_property_links
                    or row['match_status'] not in {'ambiguous','suggested','conflict'}
                    or row['confirmed_by'] is not None or row['confirmed_at'] is not None):
                raise SnapshotError('unresolved_property_small_community')
            rejected.append({'property_id':codec.reference('property',row['property_id']),
                'match_status':row['match_status'],'reason':'missing_small_community'})
            if len(rejected)>3:
                raise SnapshotError('orphan_exclusion_scope_exceeded')
            continue
        # A pre-existing conflict may be a legitimate business state. Preserve
        # separate references and the status; never guess a corrected community.
        entry_id = codec.reference("small_community", row["small_community_id"])
        output["RegistryData.registry_property_small_community_links"].append({
            "property_id": codec.reference("property", row["property_id"]),
            "small_community_id": entry_id,
            "small_community_name": codec.text("small_community_name", entry["name"], "验证小区") if entry else "",
            "community_id": codec.reference("community", row["community_id"]),
            "community_name_snapshot": community_name(row["community_id"]),
            "match_status": enum(row["match_status"], {"suggested", "confirmed", "ambiguous", "unmatched", "conflict", "invalid", "review_required", "manual_unmatched"}, empty=False),
            "confirmed_by": codec.reference("actor", row["confirmed_by"]),
            "confirmed_at": date_value(row["confirmed_at"]),
            "property_version": integer(row["property_version"], minimum=1, maximum=2**32-1),
            "match_score": 0, "match_method": "staging_snapshot", "match_reason": "脱敏副本，原匹配证据不导入",
            "matcher_version": "staging_snapshot", "match_evidence": None})
    codec.assert_tables_safe(output)
    return {"tables": output, "actors": [{"id": codec.reference("actor", value)} for value in sorted(actors)],
            "report": {"source_counts": {key: len(value) for key,value in rows.items()},
                       "output_counts": {key: len(value) for key,value in output.items()},
                       "sensitive_value_matches": 0, "reference_integrity": True,
                       "rejected_property_links": rejected,
                       "rejected_property_link_count": len(rejected),
                       "scope": "current_registry_graph", "ready_for_application_switch": False}}
