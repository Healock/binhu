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
    placeholders = ",".join(["%s"] * len(property_ids))
    await cur.execute(
        "SELECT property_id,alias FROM registry_address_aliases "
        f"WHERE enabled=1 AND property_id IN ({placeholders})",
        tuple(property_ids),
    )
    for property_id, alias in await cur.fetchall():
        variants[int(property_id)].append(str(alias or ""))
    await cur.execute(
        "SELECT property_id,natural_address,normalized_address "
        "FROM registry_property_address_versions "
        f"WHERE property_id IN ({placeholders})",
        tuple(property_ids),
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


def _match_clause(keys_by_community: dict[str, set[str]]) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    for community, keys in sorted(keys_by_community.items()):
        if not community or not keys:
            continue
        placeholders = ",".join(["%s"] * len(keys))
        clauses.append(f"(`社区`=%s AND `_address_key` IN ({placeholders}))")
        params.append(community)
        params.extend(sorted(keys))
    return " OR ".join(clauses), tuple(params)


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
    owners: dict[tuple[str, str], set[int]] = defaultdict(set)
    for item in properties:
        property_id = int(item["id"])
        community = str(item.get("community_name") or "").strip()
        for key in property_visit_keys(item, variants.get(property_id)):
            owners[(community, key)].add(property_id)

    unique_owners = {
        pair: next(iter(property_ids))
        for pair, property_ids in owners.items()
        if len(property_ids) == 1
    }
    keys_by_community: dict[str, set[str]] = defaultdict(set)
    for community, key in unique_owners:
        keys_by_community[community].add(key)
    clause, params = _match_clause(keys_by_community)
    if not clause:
        return defaults

    await cur.execute(
        "SELECT id,`社区`,`_address_key`,`业务日期`,`入户时间`,`星级`,`星级采集时间` "
        f"FROM {_visit_table()} WHERE {clause}",
        params,
    )
    latest_visit_order: dict[int, tuple[str, str, int]] = {}
    latest_rating_order: dict[int, tuple[str, int]] = {}
    for row in await cur.fetchall():
        owner = unique_owners.get((str(row[1] or "").strip(), str(row[2] or "")))
        if owner is None:
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


async def load_property_visit_history(
    cur,
    property_row: dict,
    variants: list[str],
    *,
    page: int,
    page_size: int,
) -> dict:
    keys = property_visit_keys(property_row, variants)
    community = str(property_row.get("community_name") or "").strip()
    if not community or not keys:
        return {"data": [], "total": 0, "page": page, "page_size": page_size}
    placeholders = ",".join(["%s"] * len(keys))
    where = f"`社区`=%s AND `_address_key` IN ({placeholders})"
    params: tuple[Any, ...] = (community, *sorted(keys))
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
    return {
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
