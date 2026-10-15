"""Previewable, deliberately limited consolidation of registration duplicates.

No external requests and no cross-domain writes. Houses with dependent business
relations are blocked for separate review instead of guessing how to merge them.
"""
from collections import defaultdict
from hashlib import sha256
import json

from services.registry_property_identity import current_household_snapshot, property_snapshot


def plan_identity_merges(rows: list[tuple]) -> tuple[list[tuple[int, int]], int]:
    groups = defaultdict(list)
    for row in rows:
        groups[(row[1], row[6])].append(row)
    pairs, blocked = [], 0
    for group in groups.values():
        if len(group) < 2:
            continue
        snapshots = [property_snapshot(row) for row in group]
        current = current_household_snapshot(snapshots)
        # Only source-imported active archives are eligible. Manual identities,
        # inactive archives and address-normalization collisions are not merged.
        if not current or any(row[12] != "household" or row[13] != "active" for row in group):
            blocked += 1
            continue
        pairs.extend((int(row[0]), current["id"]) for row in group if row[0] != current["id"])
    return sorted(pairs), blocked


MOVABLE_TABLES = {
    "registry_property_address_versions", "registry_address_aliases",
    "registry_property_small_community_links",
}


async def dependent_references(cur, *, schemas: list[str], registry_schema: str, old_id: int):
    await cur.execute(
        "SELECT c.TABLE_SCHEMA,c.TABLE_NAME,c.COLUMN_NAME FROM information_schema.COLUMNS c "
        "JOIN information_schema.TABLES t ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME "
        "WHERE t.TABLE_TYPE='BASE TABLE' AND c.TABLE_SCHEMA IN ("
        + ",".join(["%s"] * len(schemas)) + ") "
        "AND c.COLUMN_NAME IN ('property_id','registration_property_id')",
        tuple(schemas),
    )
    blocked = []
    for schema, table, column in await cur.fetchall():
        if schema == registry_schema and table in MOVABLE_TABLES:
            continue
        # Names come from metadata, not command input. Still quote defensively.
        quoted = [str(name).replace("`", "``") for name in (schema, table, column)]
        await cur.execute(f"SELECT 1 FROM `{quoted[0]}`.`{quoted[1]}` WHERE `{quoted[2]}`=%s LIMIT 1", (old_id,))
        if await cur.fetchone():
            blocked.append((schema, table, column))
    return blocked


async def prepare_merges(cur, *, schemas: list[str], registry_schema: str, property_ids: list[int]):
    # Lock both sides and source-link history. Applying uses the same transaction
    # and checks the measurement token before any writes.
    await cur.execute(
        "SELECT id,community_id,street,natural_address,building,room,normalized_address,"
        "source_house_no,source_updated_at,housing_type,residence_type,household_status,source_type,status,current_version "
        "FROM registry_properties ORDER BY id"
    )
    rows = list(await cur.fetchall())
    selected = set(property_ids)
    if selected:
        selected_keys = {(row[1], row[6]) for row in rows if row[0] in selected}
        rows = [row for row in rows if (row[1], row[6]) in selected_keys]
    counts = defaultdict(int)
    for row in rows:
        counts[(row[1], row[6])] += 1
    rows = [row for row in rows if counts[(row[1], row[6])] > 1]
    locked = []
    for community_id, address in sorted({(row[1], row[6]) for row in rows}, key=str):
        await cur.execute(
            "SELECT id,community_id,street,natural_address,building,room,normalized_address,"
            "source_house_no,source_updated_at,housing_type,residence_type,household_status,source_type,status,current_version "
            "FROM registry_properties WHERE community_id <=> %s AND normalized_address=%s ORDER BY id FOR UPDATE",
            (community_id, address),
        )
        locked.extend(await cur.fetchall())
    rows = locked
    pairs, blocked = plan_identity_merges(rows)
    ready = []
    evidence = [rows]
    for old_id, current_id in pairs:
        references = await dependent_references(cur, schemas=schemas, registry_schema=registry_schema, old_id=old_id)
        await cur.execute(
            "SELECT property_id,small_community_id,match_status,confirmed_by,confirmed_at "
            "FROM registry_property_small_community_links WHERE property_id IN (%s,%s) FOR UPDATE",
            (old_id, current_id),
        )
        links = list(await cur.fetchall())
        old_link = next((row for row in links if row[0] == old_id), None)
        current_link = next((row for row in links if row[0] == current_id), None)
        # Human confirmations cannot be discarded, even when they agree today.
        link_conflict = old_link and (old_link[3] is not None or old_link[4] is not None or old_link[2] == "confirmed"
                                    or (current_link and old_link[1] != current_link[1]))
        await cur.execute(
            "SELECT id,batch_id,source_ref FROM registry_source_records "
            "WHERE entity_type='household_property' AND entity_id IN (%s,%s) ORDER BY id FOR UPDATE",
            (old_id, current_id),
        )
        sources = list(await cur.fetchall())
        histories = []
        for table in ("registry_property_address_versions", "registry_address_aliases"):
            await cur.execute(f"SELECT * FROM {table} WHERE property_id IN (%s,%s) ORDER BY id FOR UPDATE", (old_id, current_id))
            histories.append(list(await cur.fetchall()))
        evidence.append([old_id, current_id, references, links, sources, histories])
        if references or link_conflict:
            blocked += 1
        else:
            ready.append((old_id, current_id))
    token = sha256(json.dumps(evidence, default=str, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return ready, blocked, token


async def apply_merge(cur, *, old_id: int, current_id: int, run_id: str):
    # Archive the complete protected property and link snapshots before removal.
    # They remain in RegistryData, never in CLI output or audit/log text.
    await cur.execute("SELECT * FROM registry_properties WHERE id=%s FOR UPDATE", (old_id,))
    columns = [column[0] for column in cur.description]
    row = await cur.fetchone()
    if row is None:
        return
    snapshot = {"property": dict(zip(columns, row))}
    await cur.execute("SELECT * FROM registry_property_small_community_links WHERE property_id=%s FOR UPDATE", (old_id,))
    columns = [column[0] for column in cur.description]
    link = await cur.fetchone()
    snapshot["small_community_link"] = dict(zip(columns, link)) if link else None
    await cur.execute(
        "INSERT INTO registry_property_identity_merges (old_property_id,canonical_property_id,snapshot_json,run_id) "
        "VALUES (%s,%s,%s,%s)",
        (old_id, current_id, json.dumps(snapshot, default=str, ensure_ascii=False), run_id),
    )
    await cur.execute("UPDATE registry_properties SET current_version=current_version+1 WHERE id=%s", (current_id,))
    await cur.execute(
        "UPDATE registry_source_records SET entity_id=%s WHERE entity_type='household_property' AND entity_id=%s",
        (current_id, old_id),
    )
    await cur.execute("SELECT COALESCE(MAX(version_no),0) FROM registry_property_address_versions WHERE property_id=%s", (current_id,))
    maximum = int((await cur.fetchone())[0])
    await cur.execute("SELECT id FROM registry_property_address_versions WHERE property_id=%s ORDER BY version_no,id FOR UPDATE", (old_id,))
    versions = list(await cur.fetchall())
    for index, (version_id,) in enumerate(versions, start=1):
        await cur.execute(
            "UPDATE registry_property_address_versions SET property_id=%s,version_no=%s WHERE id=%s",
            (current_id, maximum + index, version_id),
        )
    await cur.execute("UPDATE registry_property_address_versions SET effective_to=UTC_TIMESTAMP() WHERE property_id=%s AND effective_to IS NULL", (current_id,))
    await cur.execute(
        "INSERT INTO registry_property_address_versions "
        "(property_id,version_no,street,natural_address,building,room,normalized_address,effective_from,source_type,source_ref,change_reason) "
        "SELECT id,%s,street,natural_address,building,room,normalized_address,UTC_TIMESTAMP(),source_type,source_ref,'户号沿革归并保留当前地址' "
        "FROM registry_properties WHERE id=%s",
        (maximum + len(versions) + 1, current_id),
    )
    await cur.execute("UPDATE registry_address_aliases SET property_id=%s WHERE property_id=%s", (current_id, old_id))
    await cur.execute("SELECT 1 FROM registry_property_small_community_links WHERE property_id=%s", (current_id,))
    if await cur.fetchone():
        await cur.execute("DELETE FROM registry_property_small_community_links WHERE property_id=%s", (old_id,))
    else:
        await cur.execute("UPDATE registry_property_small_community_links SET property_id=%s WHERE property_id=%s", (current_id, old_id))
    await cur.execute(
        "UPDATE registry_property_small_community_links link JOIN registry_properties p ON p.id=link.property_id "
        "SET link.property_version=p.current_version WHERE link.property_id=%s", (current_id,),
    )
    await cur.execute("DELETE FROM registry_properties WHERE id=%s", (old_id,))
