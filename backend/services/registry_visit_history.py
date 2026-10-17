"""房屋档案与走访明细的只读关联。

房屋和走访分属 RegistryData、VisitData。这里不复制业务正文，也不建立
跨库外键；只使用社区和走访导入时生成的地址摘要做精确匹配。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from hashlib import sha256
from typing import Any

from config import settings
from services.visit_import import normalize_address as normalize_visit_address
from services.registry_import import normalize_address as normalize_registry_address
from services.registry_property_identity import current_household_snapshot, property_snapshot
from services.registry_property_canonical import load_property_identity_graph


def _iso(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value) if value is not None else None


def visit_address_key(value: Any) -> str:
    normalized = normalize_visit_address(value)
    if not normalized:
        return ""
    return sha256(normalized.encode("utf-8")).hexdigest()


def property_visit_keys(property_row: dict, variants: list[str] | None = None) -> set[str]:
    """生成可与 t_visit_details._address_key 精确比较的候选摘要。"""
    values = [
        property_row.get("natural_address"),
        property_row.get("normalized_address"),
        *(variants or []),
    ]
    return {key for value in values if (key := visit_address_key(value))}


async def load_property_address_variants(cur, property_ids: list[int]) -> dict[int, list[str]]:
    variants: dict[int, list[str]] = defaultdict(list)
    if not property_ids:
        return variants
    for start in range(0, len(property_ids), 500):
        batch = property_ids[start:start + 500]
        placeholders = ",".join(["%s"] * len(batch))
        await cur.execute(
            "SELECT property_id,alias FROM registry_address_aliases "
            f"WHERE enabled=1 AND property_id IN ({placeholders})",
            tuple(batch),
        )
        for property_id, alias in await cur.fetchall():
            variants[int(property_id)].append(str(alias or ""))
        await cur.execute(
            "SELECT property_id,natural_address,normalized_address "
            "FROM registry_property_address_versions "
            f"WHERE property_id IN ({placeholders})",
            tuple(batch),
        )
        for property_id, natural_address, normalized_address in await cur.fetchall():
            variants[int(property_id)].extend([
                str(natural_address or ""),
                str(normalized_address or ""),
            ])
    return variants


def _visit_table() -> str:
    schema = settings.MYSQL_VISIT_DB.replace("`", "")
    return f"`{schema}`.`t_visit_details`"


async def _community_labels(cur, community_ids: set[int]):
    """Resolve VisitData's text labels to stable IDs without guessing.

    Read all identities so a stale snapshot/alias shared by two communities is
    rejected, even when only one community is on the current page.
    """
    schema = (settings.MYSQL_PLATFORM_DB if settings.PLATFORM_DOMAIN_ACTIVE
              else settings.MYSQL_ONLINE_DATA_DB).replace("`", "")
    await cur.execute(f"SELECT id,name FROM `{schema}`._communities")
    formal = list(await cur.fetchall())
    valid_ids = {int(row[0]) for row in formal}
    await cur.execute(f"SELECT community_id,alias FROM `{schema}`._community_aliases")
    aliases = list(await cur.fetchall())
    await cur.execute("SELECT DISTINCT community_id,community_name_snapshot FROM registry_properties WHERE community_id IS NOT NULL")
    snapshots = list(await cur.fetchall())
    owners = defaultdict(set)
    for identifier, label in [*formal, *aliases, *snapshots]:
        label = str(label or "").strip()
        if identifier is not None and label:
            owners[label].add(int(identifier))
    labels = defaultdict(set)
    resolved = {}
    contested_ids = set()
    for label, identifiers in owners.items():
        if len(identifiers) == 1:
            identifier = next(iter(identifiers))
            if identifier in valid_ids and identifier in community_ids:
                labels[identifier].add(label)
                resolved[label] = identifier
        else:
            contested_ids.update(identifiers)
    return labels, resolved, contested_ids


async def _address_owners(cur, properties: list[dict], variants: dict[int, list[str]]):
    """Resolve ownership against all matching archives, independent of paging.

    Include aliases and address history so filtering a duplicate out of a page
    cannot turn an ambiguous address into an apparently unique association.
    """
    identity_graph = await load_property_identity_graph(cur)
    requested_ids = [int(item["id"]) for item in properties if item.get("id")]
    family_ids = sorted(identity_graph.family(requested_ids) - set(requested_ids))
    family_properties = {}
    if family_ids:
        for start in range(0, len(family_ids), 500):
            batch = family_ids[start:start + 500]
            placeholders = ",".join(["%s"] * len(batch))
            await cur.execute(
                "SELECT p.id,p.community_id,p.street,p.natural_address,p.building,p.room,"
                "p.normalized_address,p.source_house_no,p.source_updated_at,p.housing_type,"
                "p.residence_type,p.household_status,p.community_name_snapshot "
                f"FROM registry_properties p WHERE p.id IN ({placeholders})",
                tuple(batch),
            )
            for row in await cur.fetchall():
                family_properties[int(row[0])] = {
                    **property_snapshot(row), "natural_address": row[3],
                    "normalized_address": row[6], "community_name": row[12],
                }
        historical_variants = await load_property_address_variants(cur, family_ids)
        for family_id, values_for_id in historical_variants.items():
            target_id = identity_graph.resolve(family_id).canonical_id
            variants.setdefault(target_id, []).extend(values_for_id)
    all_properties = {int(item["id"]): item for item in properties if item.get("community_id")}
    all_properties.update(family_properties)
    values = {str(value) for item in all_properties.values() for value in
              [item.get("natural_address"), item.get("normalized_address"), *variants.get(int(item["id"]), [])]
              if value}
    communities = sorted({int(item["community_id"]) for item in all_properties.values()})
    if not communities:
        return {}, {}
    keys = sorted({normalize_registry_address(value) for value in values} - {""})
    for start in range(0, len(keys), 500):
        batch = keys[start:start + 500]
        slots = ",".join(["%s"] * len(batch))
        await cur.execute(
            "SELECT p.id,p.community_id,p.street,p.natural_address,p.building,p.room,"
            "p.normalized_address,p.source_house_no,p.source_updated_at,p.housing_type,"
            "p.residence_type,p.household_status,p.community_name_snapshot "
            "FROM registry_properties p WHERE p.community_id IN ("
            + ",".join(["%s"] * len(communities)) + ") AND ("
            f"p.normalized_address IN ({slots}) OR p.id IN ("
            "SELECT v.property_id FROM registry_property_address_versions v "
            f"WHERE v.normalized_address IN ({slots})) OR p.id IN ("
            "SELECT a.property_id FROM registry_address_aliases a "
            "WHERE a.enabled=1 AND a.normalized_alias IN (" + slots + ")))",
            (*communities, *batch, *batch, *batch),
        )
        for row in await cur.fetchall():
            all_properties[int(row[0])] = {
                **property_snapshot(row), "natural_address": row[3], "normalized_address": row[6],
                "community_name": row[12],
            }
    extra = sorted(set(all_properties) - {int(item["id"]) for item in properties}
                   - set(family_properties))
    variants = {**variants, **await load_property_address_variants(cur, extra)}
    # Keep historical address/version ownership intact in storage, while
    # projecting matching candidates to the current household identity.
    collapsed: dict[int, dict] = {}
    collapsed_variants: dict[int, list[str]] = defaultdict(list)
    for property_id, item in all_properties.items():
        resolution = identity_graph.resolve(property_id)
        canonical_id = resolution.canonical_id
        if property_id == canonical_id or canonical_id not in collapsed:
            collapsed[canonical_id] = {**item, "id": canonical_id}
        collapsed_variants[canonical_id].extend(variants.get(property_id, []))
        if property_id != canonical_id:
            collapsed_variants[canonical_id].extend([
                str(item.get("natural_address") or ""),
                str(item.get("normalized_address") or ""),
            ])
    all_properties = collapsed
    variants = collapsed_variants
    owners = defaultdict(set)
    for identifier, item in all_properties.items():
        for key in property_visit_keys(item, variants.get(identifier)):
            owners[(int(item["community_id"]), key)].add(identifier)
    unique = {}
    for pair, identifiers in owners.items():
        if len(identifiers) == 1:
            unique[pair] = next(iter(identifiers))
            continue
        current = current_household_snapshot([all_properties[identifier] for identifier in identifiers])
        if current:
            unique[pair] = current["id"]
    return owners, unique


async def load_property_visit_summaries(cur, properties: list[dict]) -> dict[int, dict]:
    """一次读取当前房屋页的走访摘要，避免逐房查询。"""
    defaults = {
        int(item["id"]): {
            "visit_count": 0,
            "latest_visit_date": None,
            "latest_star_rating": None,
            "latest_star_rating_at": None,
        }
        for item in properties
    }
    if not properties:
        return defaults

    variants = await load_property_address_variants(cur, list(defaults))
    owners, unique_owners = await _address_owners(cur, properties, variants)
    labels, label_ids, contested_ids = await _community_labels(cur, {pair[0] for pair in owners})
    for item in properties:
        if not labels.get(item.get("community_id")) or item.get("community_id") in contested_ids:
            defaults[int(item["id"])]["visit_association_status"] = "community_unresolved"
    for pair, identifiers in owners.items():
        for identifier in identifiers & defaults.keys():
            if pair not in unique_owners:
                defaults[identifier]["visit_association_status"] = "ambiguous"
            elif unique_owners[pair] != identifier:
                defaults[identifier]["visit_association_status"] = "historical_registration"
    keys_by_community: dict[int, set[str]] = defaultdict(set)
    for community, key in unique_owners:
        keys_by_community[community].add(key)
    if not keys_by_community:
        return defaults
    latest_visit_order: dict[int, tuple[str, str, int]] = {}
    latest_rating_order: dict[int, tuple[str, int]] = {}
    for community, key_set in sorted(keys_by_community.items()):
        names = sorted(labels.get(community, []))
        if not names:
            continue
        keys = sorted(key_set)
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            placeholders = ",".join(["%s"] * len(batch))
            await cur.execute(
                "SELECT id,`社区`,`_address_key`,`业务日期`,`入户时间`,`星级`,`星级采集时间` "
                f"FROM {_visit_table()} WHERE `社区` IN ({','.join(['%s'] * len(names))}) AND `_address_key` IN ({placeholders})",
                (*names, *batch),
            )
            for row in await cur.fetchall():
                owner = unique_owners.get((label_ids.get(str(row[1] or "").strip()), str(row[2] or "")))
                if owner is None or owner not in defaults:
                    continue
                summary = defaults[owner]
                summary["visit_count"] += 1
                visit_date = _iso(row[3]) or ""
                visit_at = _iso(row[4]) or ""
                visit_order = (visit_date, visit_at, int(row[0]))
                if visit_order > latest_visit_order.get(owner, ("", "", 0)):
                    latest_visit_order[owner] = visit_order
                    summary["latest_visit_date"] = visit_date or None
                star = str(row[5] or "").strip()
                if star:
                    star_at = _iso(row[6]) or visit_at
                    rating_order = (star_at, int(row[0]))
                    if rating_order > latest_rating_order.get(owner, ("", 0)):
                        latest_rating_order[owner] = rating_order
                        summary["latest_star_rating"] = star
                        summary["latest_star_rating_at"] = star_at or None
    return defaults


async def filter_property_ids_by_visit(
    cur,
    properties: list[dict],
    *,
    visit_start_date: date | None = None,
    visit_end_date: date | None = None,
    star_ratings: list[str] | None = None,
    visit_status: str = "",
) -> set[int]:
    """Return properties having at least one matching visit record.

    Address matching intentionally reuses the same exact community/address-key
    rules as the summary loader.  The caller supplies the complete candidate
    property set before pagination, so filtering cannot accidentally affect only
    the currently displayed page.
    """
    if not properties:
        return set()
    variants = await load_property_address_variants(cur, [int(item["id"]) for item in properties])
    owners, unique_owners = await _address_owners(cur, properties, variants)
    labels, label_ids, contested_ids = await _community_labels(cur, {pair[0] for pair in owners})
    keys_by_community: dict[int, set[str]] = defaultdict(set)
    for community, key in unique_owners:
        keys_by_community[community].add(key)
    if not keys_by_community and visit_status != "never":
        return set()
    # For absence, any matching historical visit prevents a "never" label,
    # even when the address is shared by multiple candidate properties.
    if visit_status == "never":
        keys_by_community = defaultdict(set)
        for community, key in owners:
            keys_by_community[community].add(key)
    normalized_ratings = [str(value).strip() for value in (star_ratings or []) if str(value).strip()]
    matched: set[int] = set()
    for community, key_set in sorted(keys_by_community.items()):
        names = sorted(labels.get(community, []))
        if not names:
            continue
        keys = sorted(key_set)
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            placeholders = ",".join(["%s"] * len(batch))
            conditions = [f"`社区` IN ({','.join(['%s'] * len(names))})", f"`_address_key` IN ({placeholders})"]
            query_params: list[Any] = [*names, *batch]
            if visit_start_date is not None and visit_status != "never":
                conditions.append("`业务日期` >= %s")
                query_params.append(visit_start_date)
            if visit_end_date is not None and visit_status != "never":
                conditions.append("`业务日期` <= %s")
                query_params.append(visit_end_date)
            if normalized_ratings and visit_status != "never":
                rating_placeholders = ",".join(["%s"] * len(normalized_ratings))
                conditions.append(f"`星级` IN ({rating_placeholders})")
                query_params.extend(normalized_ratings)
            await cur.execute(
                f"SELECT DISTINCT `社区`,`_address_key` FROM {_visit_table()} WHERE {' AND '.join(conditions)}",
                tuple(query_params),
            )
            for visit_community, address_key in await cur.fetchall():
                pair = (label_ids.get(str(visit_community or "").strip()), str(address_key or ""))
                if visit_status == "never":
                    matched.update(owners.get(pair, set()))
                    continue
                owner = unique_owners.get(pair)
                if owner is not None:
                    matched.add(owner)
    if visit_status == "never":
        return {int(item["id"]) for item in properties
                if labels.get(item.get("community_id")) and item.get("community_id") not in contested_ids} - matched
    return matched & {int(item["id"]) for item in properties}


async def load_property_visit_history(
    cur,
    property_row: dict,
    variants: list[str],
    *,
    page: int,
    page_size: int,
) -> dict:
    keys = property_visit_keys(property_row, variants)
    community = property_row.get("community_id")
    labels, _, contested_ids = await _community_labels(cur, {community} if community else set())
    names = sorted(labels.get(community, []))
    if not names:
        return {"data": [], "total": 0, "page": page, "page_size": page_size,
                "visit_association_status": "community_unresolved"}
    if property_row.get("id"):
        owners, unique = await _address_owners(cur, [property_row], {int(property_row["id"]): variants})
        relevant = [(community, key) for key in keys]
        keys = {key for key in keys if unique.get((community, key)) == int(property_row["id"])}
        if not keys:
            ambiguous = any(pair not in unique for pair in relevant if pair in owners)
            return {"data": [], "total": 0, "page": page, "page_size": page_size,
                    "visit_association_status": "ambiguous" if ambiguous else "historical_registration"}
    if not community or not keys:
        return {"data": [], "total": 0, "page": page, "page_size": page_size}
    placeholders = ",".join(["%s"] * len(keys))
    where = f"`社区` IN ({','.join(['%s'] * len(names))}) AND `_address_key` IN ({placeholders})"
    params: tuple[Any, ...] = (*names, *sorted(keys))
    await cur.execute(f"SELECT COUNT(*) FROM {_visit_table()} WHERE {where}", params)
    total = int((await cur.fetchone())[0])
    await cur.execute(
        "SELECT id,`社区`,`进入方式`,`地址`,`操作人`,`入户时间`,`业务日期`,"
        "`房间核查数量`,`新增`,`变更`,`注销`,`星级`,`得分`,`星级采集时间`,`星级采集日期` "
        f"FROM {_visit_table()} WHERE {where} "
        "ORDER BY `业务日期` DESC,`入户时间` DESC,id DESC LIMIT %s OFFSET %s",
        (*params, page_size, (page - 1) * page_size),
    )
    rows = await cur.fetchall()
    result = {
        "data": [
            {
                "id": int(row[0]),
                "community": row[1],
                "entry_method": row[2],
                "address": row[3],
                "operator_name": row[4],
                "visited_at": _iso(row[5]),
                "business_date": _iso(row[6]),
                "room_check_count": int(row[7] or 0),
                "added_count": int(row[8] or 0),
                "changed_count": int(row[9] or 0),
                "cancelled_count": int(row[10] or 0),
                "star_rating": row[11],
                "score": float(row[12]) if row[12] is not None else None,
                "star_rated_at": _iso(row[13]),
                "star_rating_date": _iso(row[14]),
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    if not total and community in contested_ids:
        result["visit_association_status"] = "community_unresolved"
    return result
