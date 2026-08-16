"""Resolve a QMF community from platform community and address records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from services.police_dispatch import normalize_community_label, normalize_lookup


@dataclass(frozen=True)
class QmfCommunity:
    id: int
    name: str
    qmf_community_code: str


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


async def resolve_qmf_community(
    cur,
    *,
    source_community: str,
    address: str,
) -> QmfCommunity:
    """Resolve one enabled community; ambiguity is never guessed."""
    await cur.execute(
        """
        SELECT community.id, community.name, community.qmf_community_code
        FROM _communities AS community
        WHERE community.is_active=1
          AND EXISTS (
              SELECT 1 FROM _departments AS department
              WHERE department.community_id=community.id
                AND department.department_type='community'
                AND department.is_active=1
          )
        ORDER BY community.id
        """
    )
    communities = {
        int(row[0]): {
            "id": int(row[0]),
            "name": str(row[1] or "").strip(),
            "qmf_community_code": str(row[2] or "").strip(),
            "aliases": [],
        }
        for row in await cur.fetchall()
    }
    if not communities:
        raise ValueError("no_enabled_community")

    await cur.execute(
        "SELECT community_id, alias FROM _community_aliases ORDER BY id"
    )
    for community_id, alias in await cur.fetchall():
        item = communities.get(int(community_id))
        if item is not None:
            item["aliases"].append(str(alias or "").strip())

    direct_ids: set[int] = set()
    source_key = normalize_community_label(source_community)
    if source_key:
        for community_id, item in communities.items():
            labels = [item["name"], *item["aliases"]]
            if any(normalize_community_label(label) == source_key for label in labels):
                direct_ids.add(community_id)

    address_ids: set[int] = set()
    address_key = normalize_lookup(address)
    if address_key:
        await cur.execute(
            """
            SELECT entry.community_id, entry.name, entry.detail_address,
                   entry.aliases_json
            FROM _police_address_entries AS entry
            WHERE entry.enabled=1 AND entry.community_id IS NOT NULL
            ORDER BY entry.id
            """
        )
        for community_id, name, detail_address, aliases_json in await cur.fetchall():
            resolved_id = int(community_id)
            if resolved_id not in communities:
                continue
            tokens = [
                str(name or ""),
                str(detail_address or ""),
                *_json_list(aliases_json),
            ]
            if any(
                len(token_key) >= 2 and token_key in address_key
                for token_key in (normalize_lookup(token) for token in tokens)
                if token_key
            ):
                address_ids.add(resolved_id)

    if len(direct_ids) > 1 or len(address_ids) > 1:
        raise ValueError("community_ambiguous")
    if direct_ids and address_ids and direct_ids != address_ids:
        raise ValueError("community_conflict")
    resolved_ids = direct_ids or address_ids
    if len(resolved_ids) != 1:
        raise ValueError("community_not_found")

    item = communities[next(iter(resolved_ids))]
    code = str(item["qmf_community_code"] or "")
    if not re.fullmatch(r"\d{10}", code):
        raise ValueError("community_code_missing")
    return QmfCommunity(id=item["id"], name=item["name"], qmf_community_code=code)
