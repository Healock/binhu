"""Build privacy-minimized evidence for household-property identity reviews."""

from collections import defaultdict
from hashlib import sha256
import json


HISTORICAL_REFERENCE_TABLES = {
    "registry_property_address_versions",
    "registry_address_aliases",
    "registry_property_small_community_links",
    "registry_source_records",
}
POLYMORPHIC_REFERENCE_TABLES = {
    "registry_source_records": ("household_property", "property"),
    "registry_change_candidates": ("property",),
}
IDENTITY_MAINTENANCE_AUDIT_TABLES = {
    "registry_property_identity_mappings",
    "registry_property_identity_events",
    "registry_property_identity_reference_events",
}
KNOWN_ENTITY_TYPES = {
    "household_property",
    "property_certificate",
    "property",
    "person",
    "organization",
}
WORK_ORDER_OBJECT_TYPES = {
    "property",
    "household_property",
    "registry_property",
    "property_certificate",
    "house",
}


def _quote(identifier: str) -> str:
    return "`" + str(identifier).replace("`", "``") + "`"


def _json_contains_property_scalar(value, property_id: int) -> bool:
    """Recursively match exact numeric or string property-id scalars."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value == int(property_id)
    if isinstance(value, str):
        return value == str(int(property_id))
    if isinstance(value, list):
        return any(_json_contains_property_scalar(item, property_id) for item in value)
    if isinstance(value, dict):
        return any(_json_contains_property_scalar(item, property_id) for item in value.values())
    return False


async def discover_reference_inventory(cur, schemas: list[str], registry_schema: str) -> tuple[list[dict], list[dict], list[dict]]:
    placeholders = ",".join(["%s"] * len(schemas))
    await cur.execute(
        "SELECT c.TABLE_SCHEMA,c.TABLE_NAME,c.COLUMN_NAME,c.DATA_TYPE "
        "FROM information_schema.COLUMNS c "
        "JOIN information_schema.TABLES t ON t.TABLE_SCHEMA=c.TABLE_SCHEMA "
        "AND t.TABLE_NAME=c.TABLE_NAME AND t.TABLE_TYPE='BASE TABLE' "
        f"WHERE c.TABLE_SCHEMA IN ({placeholders})",
        tuple(schemas),
    )
    columns = list(await cur.fetchall())
    await cur.execute(
        "SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE CONSTRAINT_NAME='PRIMARY' "
        f"AND TABLE_SCHEMA IN ({placeholders}) ORDER BY TABLE_SCHEMA,TABLE_NAME,ORDINAL_POSITION",
        tuple(schemas),
    )
    primary_keys = defaultdict(list)
    for schema, table, column in await cur.fetchall():
        primary_keys[(str(schema), str(table))].append(str(column))
    await cur.execute(
        "SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE REFERENCED_TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME='registry_properties' "
        f"AND TABLE_SCHEMA IN ({placeholders})",
        (registry_schema, *schemas),
    )
    foreign_keys = {(row[0], row[1], row[2]) for row in await cur.fetchall()}

    inventory = []
    opaque_json = []
    polymorphic_warnings = []
    work_order_link_tables = set()
    for schema, table, column, data_type in columns:
        schema, table, column = str(schema), str(table), str(column)
        # The identity graph and its audit rows describe the resolver itself;
        # they are not business references that should block a later measure.
        if schema == registry_schema and table in IDENTITY_MAINTENANCE_AUDIT_TABLES:
            continue
        table_primary_keys = primary_keys.get((schema, table), [])
        key = (schema, table, column)
        lower = column.lower()
        entity_types = POLYMORPHIC_REFERENCE_TABLES.get(table) if schema == registry_schema else None
        is_poly = bool(entity_types)
        if table == "work_order_links" and column in {"object_id", "object_type"}:
            work_order_link_tables.add((schema, table))
        if table == "work_order_links" and column == "object_id":
            inventory.append({
                "schema": schema,
                "table": table,
                "column": column,
                "primary_keys": list(table_primary_keys),
                "kind": "work_order_link",
                "entity_type": None,
                "reference_type_column": "object_type",
            })
            continue
        is_reference = (
            lower == "property_id"
            or lower == "registration_property_id"
            or lower.endswith("_property_id")
            or key in foreign_keys
            or (column == "entity_id" and is_poly)
        )
        if data_type.lower() == "json":
            opaque_json.append({"schema": schema, "table": table, "column": column})
        if not is_reference:
            continue
        discriminators = entity_types if column == "entity_id" and entity_types else (None,)
        for discriminator in discriminators:
            inventory.append({
                "schema": schema,
                "table": table,
                "column": column,
                "primary_keys": list(table_primary_keys),
                "kind": ("historical_polymorphic" if table == "registry_source_records" else "polymorphic") if discriminator else (
                    "historical" if schema == registry_schema and table in HISTORICAL_REFERENCE_TABLES
                    else "direct"
                ),
                "entity_type": discriminator,
            })
    inventory.sort(key=lambda item: (item["schema"], item["table"], item["column"]))
    opaque_json.sort(key=lambda item: (item["schema"], item["table"], item["column"]))
    for table in POLYMORPHIC_REFERENCE_TABLES:
        if (registry_schema, table, "entity_id") not in {(item["schema"], item["table"], item["column"]) for item in inventory}:
            continue
        await cur.execute(
            f"SELECT entity_type,COUNT(*) FROM {_quote(registry_schema)}.{_quote(table)} "
            "WHERE entity_id IS NOT NULL GROUP BY entity_type"
        )
        for entity_type, count in await cur.fetchall():
            if str(entity_type or "") not in KNOWN_ENTITY_TYPES:
                polymorphic_warnings.append({
                    "schema": registry_schema,
                    "table": table,
                    "column": "entity_id",
                    "entity_type": str(entity_type or ""),
                    "count": int(count),
                    "warning": "unknown_entity_type_blocks_review",
                })
    for schema, table in sorted(work_order_link_tables):
        columns_for_table = {
            item[2] for item in columns
            if str(item[0]) == schema and str(item[1]) == table
        }
        if not {"object_id", "object_type"}.issubset(columns_for_table):
            polymorphic_warnings.append({
                "schema": schema,
                "table": table,
                "column": "object_id",
                "entity_type": "",
                "count": 0,
                "warning": "work_order_link_schema_incomplete_blocks_review",
            })
            continue
        await cur.execute(
            f"SELECT object_type,COUNT(*) FROM {_quote(schema)}.{_quote(table)} "
            "WHERE object_type IS NOT NULL GROUP BY object_type"
        )
        for object_type, count in await cur.fetchall():
            normalized = str(object_type or "").strip().lower()
            if normalized not in WORK_ORDER_OBJECT_TYPES:
                polymorphic_warnings.append({
                    "schema": schema,
                    "table": table,
                    "column": "object_type",
                    "entity_type": str(object_type or ""),
                    "count": int(count),
                    "warning": "unknown_work_order_object_type_blocks_review",
                })
    polymorphic_warnings.sort(key=lambda item: (item["schema"], item["table"], item.get("entity_type", "")))
    return inventory, opaque_json, polymorphic_warnings


async def scan_references(cur, inventory: list[dict], property_ids: list[int], *, chunk_size: int = 200) -> list[dict]:
    if not property_ids:
        return []
    grouped = defaultdict(lambda: {"count": 0, "keys": []})
    for item in inventory:
        if item.get("kind") == "work_order_link":
            type_column = item["reference_type_column"]
            for start in range(0, len(property_ids), chunk_size):
                chunk = property_ids[start:start + chunk_size]
                value_placeholders = ",".join(["%s"] * len(chunk))
                selected = [item["column"], type_column, *item["primary_keys"]]
                sql = "SELECT " + ",".join(_quote(name) for name in selected)
                sql += f" FROM {_quote(item['schema'])}.{_quote(item['table'])}"
                sql += f" WHERE {_quote(item['column'])} IN ({value_placeholders})"
                await cur.execute(sql, tuple(str(value) for value in chunk))
                for row in await cur.fetchall():
                    try:
                        property_id = int(row[0])
                    except (TypeError, ValueError):
                        continue
                    object_type = str(row[1] or "")
                    key = (property_id, item["schema"], item["table"], item["column"], item["kind"], object_type)
                    grouped[key]["count"] += 1
                    grouped[key]["keys"].append(tuple(str(value) for value in row[2:]))
            continue
        if not item["primary_keys"]:
            # Count unkeyed references without exposing row contents; their presence blocks later review.
            for start in range(0, len(property_ids), chunk_size):
                chunk = property_ids[start:start + chunk_size]
                value_placeholders = ",".join(["%s"] * len(chunk))
                sql = "SELECT " + _quote(item["column"]) + ",COUNT(*)"
                sql += f" FROM {_quote(item['schema'])}.{_quote(item['table'])}"
                predicates = [f"{_quote(item['column'])} IN ({value_placeholders})"]
                params = list(chunk)
                if item["entity_type"]:
                    predicates.append("`entity_type`=%s")
                    params.append(item["entity_type"])
                sql += " WHERE " + " AND ".join(predicates) + " GROUP BY " + _quote(item["column"])
                await cur.execute(sql, tuple(params))
                for property_id, count in await cur.fetchall():
                    key = (int(property_id), item["schema"], item["table"], item["column"], item["kind"], item["entity_type"])
                    grouped[key]["count"] += int(count)
                    grouped[key]["unkeyed"] = True
            continue
        for start in range(0, len(property_ids), chunk_size):
            chunk = property_ids[start:start + chunk_size]
            value_placeholders = ",".join(["%s"] * len(chunk))
            selected = [item["column"], *item["primary_keys"]]
            sql = "SELECT " + ",".join(_quote(name) for name in selected)
            sql += f" FROM {_quote(item['schema'])}.{_quote(item['table'])}"
            predicates = [f"{_quote(item['column'])} IN ({value_placeholders})"]
            params = list(chunk)
            if item["entity_type"]:
                predicates.append("`entity_type`=%s")
                params.append(item["entity_type"])
            sql += " WHERE " + " AND ".join(predicates)
            await cur.execute(sql, tuple(params))
            for row in await cur.fetchall():
                property_id = int(row[0])
                key = (property_id, item["schema"], item["table"], item["column"], item["kind"], item["entity_type"])
                grouped[key]["count"] += 1
                grouped[key]["keys"].append(tuple(str(value) for value in row[1:]))

    findings = []
    for (property_id, schema, table, column, kind, entity_type), data in sorted(grouped.items()):
        if not data["count"] and not data.get("unkeyed"):
            continue
        key_digest = None
        if data["keys"]:
            canonical = json.dumps(sorted(data["keys"]), ensure_ascii=False, separators=(",", ":"))
            key_digest = sha256(canonical.encode("utf-8")).hexdigest()
        findings.append({
            "property_id": property_id,
            "schema": schema,
            "table": table,
            "column": column,
            "kind": kind,
            "entity_type": entity_type,
            "count": data["count"],
            "record_key_sha256": key_digest,
            "stable_record_keys": not data.get("unkeyed", False),
        })
    return findings


async def scan_certificate_source_references(
    cur, *, registry_schema: str, property_ids: list[int], chunk_size: int = 200
) -> list[dict]:
    """Resolve certificate source-record IDs to their property without exposing payloads."""
    findings = []
    for start in range(0, len(property_ids), chunk_size):
        chunk = property_ids[start:start + chunk_size]
        placeholders = ",".join(["%s"] * len(chunk))
        await cur.execute(
            "SELECT certificate.property_id,source.id,certificate.id "
            f"FROM {_quote(registry_schema)}.registry_property_certificates certificate "
            f"JOIN {_quote(registry_schema)}.registry_source_records source "
            "ON source.entity_type='property_certificate' AND source.entity_id=certificate.id "
            "WHERE certificate.property_id IN (" + placeholders + ")"
            " AND certificate.source_type='certificate'",
            tuple(chunk),
        )
        grouped = defaultdict(list)
        for property_id, source_id, certificate_id in await cur.fetchall():
            grouped[int(property_id)].append((str(source_id), str(certificate_id)))
        for property_id, keys in sorted(grouped.items()):
            canonical = json.dumps(sorted(keys), ensure_ascii=False, separators=(",", ":"))
            findings.append({
                "property_id": property_id,
                "schema": registry_schema,
                "table": "registry_source_records",
                "column": "entity_id",
                "kind": "certificate_source_reference",
                "entity_type": "property_certificate",
                "count": len(keys),
                "record_key_sha256": sha256(canonical.encode("utf-8")).hexdigest(),
                "stable_record_keys": True,
            })
    return findings


async def scan_opaque_json_reference_markers(
    cur, opaque_json: list[dict], property_ids: list[int]
) -> list[dict]:
    """Find exact candidate IDs anywhere in JSON documents.

    The scan deliberately does not infer field names. JSON values are parsed
    recursively in memory so numeric and string scalars are distinguished from
    surrounding text. A match is a blocker; payloads and row identifiers are
    never returned or logged. A malformed value blocks all candidate IDs for
    that column rather than being treated as empty.
    """
    findings = []
    for item in opaque_json:
        schema, table, column = (str(item[key]) for key in ("schema", "table", "column"))
        candidates = sorted(set(int(value) for value in property_ids))
        await cur.execute(
            "SELECT /*+ MAX_EXECUTION_TIME(5000) */ "
            f"{_quote(column)} FROM {_quote(schema)}.{_quote(table)} "
            f"WHERE {_quote(column)} IS NOT NULL"
        )
        counts = {property_id: 0 for property_id in candidates}
        parse_failed = False
        for row in await cur.fetchall():
            value = row[0] if isinstance(row, (tuple, list)) else row
            try:
                parsed = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
                for property_id in candidates:
                    if _json_contains_property_scalar(parsed, property_id):
                        counts[property_id] += 1
            except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                parse_failed = True
                break
        if parse_failed:
            counts = {property_id: 1 for property_id in candidates}
        for property_id in candidates:
            count = counts[property_id]
            findings.append({
                "property_id": property_id,
                "schema": schema,
                "table": table,
                "column": column,
                "count": count,
                "key_pattern": "parsed_nested_numeric_or_string_scalar" if not parse_failed else "json_parse_failed",
            })
    return findings


def seal_artifact(payload: dict) -> dict:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**payload, "artifact_sha256": sha256(canonical.encode("utf-8")).hexdigest()}


def verify_artifact(artifact: dict) -> bool:
    expected = artifact.get("artifact_sha256")
    payload = {key: value for key, value in artifact.items() if key != "artifact_sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return isinstance(expected, str) and sha256(canonical.encode("utf-8")).hexdigest() == expected
