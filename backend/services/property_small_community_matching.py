"""房屋档案与小区地址库的幂等规则匹配。

房屋原始地址、历史地址和别名都只作为候选特征，不要求遵循固定格式。
人工确认的关联永远不会被维护重跑覆盖。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from services.address_matching import RuleMatcher


STATUS_PRIORITY = {
    "confirmed": 6,
    "suggested": 5,
    "ambiguous": 4,
    "conflict": 3,
    "invalid": 2,
    "unmatched": 1,
}


def _json(value: Any, fallback):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _property_addresses(property_row: dict[str, Any]) -> list[str]:
    values = [
        property_row.get("normalized_address"),
        "".join(str(property_row.get(key) or "") for key in ("street", "natural_address", "building", "room")),
        property_row.get("natural_address"),
        *(property_row.get("history_addresses") or []),
        *(property_row.get("aliases") or []),
    ]
    return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def match_property(
    property_row: dict[str, Any],
    address_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    matcher = RuleMatcher()
    results = [
        matcher.match(
            address,
            address_entries,
            community_name=str(property_row.get("community_name") or ""),
            street_name=str(property_row.get("street") or ""),
        )
        for address in _property_addresses(property_row)
    ]
    if not results:
        return {
            "status": "unmatched", "score": 0.0, "method": "rule",
            "reason": "房屋档案没有可用于匹配的地址", "candidate": None,
            "candidates": [], "version": matcher.version,
        }
    return max(
        results,
        key=lambda item: (
            STATUS_PRIORITY.get(str(item.get("status") or "unmatched"), 0),
            float(item.get("score") or 0),
        ),
    )


async def load_address_entries(cur) -> list[dict[str, Any]]:
    await cur.execute(
        """
        SELECT entry.id, entry.name, entry.detail_address, entry.aliases_json,
               entry.community_id, community.name, entry.enabled
        FROM _police_address_entries AS entry
        LEFT JOIN _communities AS community ON community.id=entry.community_id
        ORDER BY entry.id
        """
    )
    return [
        {
            "id": int(row[0]),
            "name": str(row[1] or ""),
            "detail_address": str(row[2] or ""),
            "aliases": _json(row[3], []),
            "community_id": int(row[4]) if row[4] is not None else None,
            "community_name": str(row[5] or ""),
            "enabled": bool(row[6]),
        }
        for row in await cur.fetchall()
    ]


async def load_registry_properties(cur) -> list[dict[str, Any]]:
    await cur.execute(
        """
        SELECT id, street, community_id, community_name_snapshot,
               natural_address, building, room, normalized_address,
               current_version, status
        FROM registry_properties
        WHERE status='active'
        ORDER BY id
        """
    )
    properties = {
        int(row[0]): {
            "id": int(row[0]), "street": str(row[1] or ""),
            "community_id": int(row[2]) if row[2] is not None else None,
            "community_name": str(row[3] or ""),
            "natural_address": str(row[4] or ""),
            "building": str(row[5] or ""), "room": str(row[6] or ""),
            "normalized_address": str(row[7] or ""),
            "version": int(row[8] or 1), "status": str(row[9] or ""),
            "history_addresses": [], "aliases": [],
        }
        for row in await cur.fetchall()
    }
    if not properties:
        return []
    ids = list(properties)
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        placeholders = ",".join(["%s"] * len(batch))
        await cur.execute(
            f"""
            SELECT property_id, normalized_address, natural_address, street,
                   building, room
            FROM registry_property_address_versions
            WHERE property_id IN ({placeholders})
            ORDER BY property_id, version_no DESC
            """,
            batch,
        )
        for property_id, normalized, natural, street, building, room in await cur.fetchall():
            value = str(normalized or "").strip() or "".join(
                str(item or "") for item in (street, natural, building, room)
            )
            if value:
                properties[int(property_id)]["history_addresses"].append(value)
        await cur.execute(
            f"""
            SELECT property_id, alias
            FROM registry_address_aliases
            WHERE property_id IN ({placeholders}) AND enabled=1
            ORDER BY property_id, id
            """,
            batch,
        )
        for property_id, alias in await cur.fetchall():
            if str(alias or "").strip():
                properties[int(property_id)]["aliases"].append(str(alias).strip())
    return list(properties.values())


async def run_property_matching(
    registry_cur,
    address_entries: list[dict[str, Any]],
    *,
    apply: bool,
) -> dict[str, Any]:
    properties = await load_registry_properties(registry_cur)
    await registry_cur.execute(
        """
        SELECT property_id, match_status, small_community_id
        FROM registry_property_small_community_links
        """
    )
    existing = {
        int(row[0]): {"status": str(row[1] or ""), "entry_id": row[2]}
        for row in await registry_cur.fetchall()
    }
    counts: Counter[str] = Counter()
    rows: list[tuple[Any, ...]] = []
    disabled_confirmed_ids: list[int] = []
    for property_row in properties:
        current = existing.get(int(property_row["id"]))
        if current and current["status"] == "confirmed":
            if not any(
                int(item["id"]) == int(current.get("entry_id") or 0) and item.get("enabled")
                for item in address_entries
            ):
                counts["disabled"] += 1
                disabled_confirmed_ids.append(int(property_row["id"]))
            else:
                counts["confirmed_preserved"] += 1
            continue
        result = match_property(property_row, address_entries)
        status = str(result.get("status") or "unmatched")
        counts[status] += 1
        candidate = result.get("candidate") or {}
        rows.append((
            int(property_row["id"]), candidate.get("entry_id"),
            str(candidate.get("name") or ""), candidate.get("community_id"),
            str(candidate.get("community_name") or ""), status,
            float(result.get("score") or 0), str(result.get("method") or ""),
            str(result.get("reason") or ""),
            json.dumps({"candidates": result.get("candidates") or []}, ensure_ascii=False),
            str(result.get("version") or ""), int(property_row["version"] or 1),
        ))
    if apply and rows:
        await registry_cur.executemany(
            """
            INSERT INTO registry_property_small_community_links (
                property_id, small_community_id, small_community_name,
                community_id, community_name_snapshot, match_status,
                match_score, match_method, match_reason, match_evidence,
                matcher_version, property_version
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                small_community_id=IF(match_status='confirmed',small_community_id,VALUES(small_community_id)),
                small_community_name=IF(match_status='confirmed',small_community_name,VALUES(small_community_name)),
                community_id=IF(match_status='confirmed',community_id,VALUES(community_id)),
                community_name_snapshot=IF(match_status='confirmed',community_name_snapshot,VALUES(community_name_snapshot)),
                match_status=IF(match_status='confirmed',match_status,VALUES(match_status)),
                match_score=IF(match_status='confirmed',match_score,VALUES(match_score)),
                match_method=IF(match_status='confirmed',match_method,VALUES(match_method)),
                match_reason=IF(match_status='confirmed',match_reason,VALUES(match_reason)),
                match_evidence=IF(match_status='confirmed',match_evidence,VALUES(match_evidence)),
                matcher_version=IF(match_status='confirmed',matcher_version,VALUES(matcher_version)),
                property_version=VALUES(property_version)
            """,
            rows,
        )
    if apply and disabled_confirmed_ids:
        for start in range(0, len(disabled_confirmed_ids), 500):
            batch = disabled_confirmed_ids[start:start + 500]
            placeholders = ",".join(["%s"] * len(batch))
            await registry_cur.execute(
                f"""
                UPDATE registry_property_small_community_links
                SET match_status='disabled',
                    match_reason='已人工确认的小区已停用或不存在，需要重新确认'
                WHERE property_id IN ({placeholders}) AND match_status='confirmed'
                """,
                batch,
            )
    return {
        "properties": len(properties),
        "processed": len(rows),
        "apply": apply,
        **{key: int(value) for key, value in sorted(counts.items())},
    }


async def verify_property_matching(registry_cur) -> dict[str, Any]:
    await registry_cur.execute(
        """
        SELECT match_status, COUNT(*)
        FROM registry_property_small_community_links
        GROUP BY match_status
        """
    )
    counts = {str(row[0] or "unmatched"): int(row[1]) for row in await registry_cur.fetchall()}
    await registry_cur.execute(
        """
        SELECT COUNT(*)
        FROM registry_property_small_community_links AS link
        JOIN registry_properties AS property ON property.id=link.property_id
        WHERE property.status='active' AND link.property_version<>property.current_version
        """
    )
    stale = int((await registry_cur.fetchone())[0] or 0)
    return {"counts": counts, "stale_property_versions": stale, "consistent": stale == 0}
