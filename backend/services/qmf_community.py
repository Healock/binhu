"""Resolve a QMF community from platform community and address records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from services.police_dispatch import normalize_community_label, normalize_lookup


QMF_COMMUNITY_CODE_PATTERN = re.compile(r"[0-9A-Z]{10}")
QMF_COMMUNITY_CODE_SEED_MARKER = "migration_qmf_community_codes_20260817"
# Source confirmed on 2026-08-17: 苏州居住证平台12个社区代码.csv.
DEFAULT_QMF_COMMUNITY_CODES = {
    "三船港": "320584037C",
    "冬梅": "3205840377",
    "江城": "320584037G",
    "长板": "3205840378",
    "湖滨华城": "3205840376",
    "祥泰": "320584021E",
    "南厍": "3205840371",
    "水秀": "3205840379",
    "顾家荡": "320584037D",
    "联团": "320584037F",
    "龙河": "320584037A",
    "阅湖": "320584037E",
}


def normalize_qmf_organization_code(value: Any) -> str:
    """Normalize the legacy model-three organization/distribution code."""
    return str(value or "").strip().upper()


def normalize_qmf_community_code(value: Any) -> str:
    """Normalize the verified QMF option code without changing its namespace."""
    return str(value or "").strip().upper()


def valid_qmf_community_code(value: Any) -> bool:
    return bool(QMF_COMMUNITY_CODE_PATTERN.fullmatch(
        normalize_qmf_community_code(value)
    ))


async def seed_default_qmf_community_codes(cur) -> bool:
    """Apply the verified defaults once without restoring later admin changes."""
    await cur.execute("START TRANSACTION")
    try:
        await cur.execute(
            """
            SELECT config_value
            FROM _system_config
            WHERE config_key=%s
            FOR UPDATE
            """,
            (QMF_COMMUNITY_CODE_SEED_MARKER,),
        )
        if await cur.fetchone():
            await cur.execute("COMMIT")
            return False

        for community_name, community_code in DEFAULT_QMF_COMMUNITY_CODES.items():
            await cur.execute(
                """
                UPDATE _communities
                SET qmf_community_code=%s
                WHERE name=%s
                  AND (qmf_community_code IS NULL OR qmf_community_code='')
                """,
                (community_code, community_name),
            )
        await cur.execute(
            """
            INSERT INTO _system_config (config_key, config_value)
            VALUES (%s, %s)
            """,
            (QMF_COMMUNITY_CODE_SEED_MARKER, "0.21.9"),
        )
        await cur.execute("COMMIT")
        return True
    except Exception:
        await cur.execute("ROLLBACK")
        raise


@dataclass(frozen=True)
class QmfCommunity:
    id: int
    name: str
    qmf_community_code: str


@dataclass(frozen=True)
class QmfOrganizationResolution:
    community_id: int | None
    community_name: str
    organization_code: str
    state: str
    reason: str = ""


async def resolve_qmf_organization(
    cur,
    *,
    organization_code: str,
    source_community: str = "",
) -> QmfOrganizationResolution:
    """Resolve a legacy 12-digit organization code without guessing.

    Organization codes and QMF community codes belong to different
    namespaces.  A configured organization mapping wins; the source label
    is only a safe alias/name fallback when the code is absent.
    """
    code = normalize_qmf_organization_code(organization_code)
    source_key = normalize_community_label(source_community)
    await cur.execute(
        """
        SELECT c.id, c.name, a.alias
        FROM _communities AS c
        LEFT JOIN _community_aliases AS a ON a.community_id=c.id
        WHERE c.is_active=1
        ORDER BY c.id, a.id
        """
    )
    communities: dict[int, dict[str, Any]] = {}
    for row in await cur.fetchall():
        item = communities.setdefault(int(row[0]), {
            "id": int(row[0]), "name": str(row[1] or "").strip(),
            "aliases": [],
        })
        if row[2]:
            item["aliases"].append(str(row[2]).strip())

    if code:
        await cur.execute(
            """
            SELECT c.id, c.name
            FROM _qmf_organization_codes AS code
            JOIN _communities AS c ON c.id=code.community_id
            WHERE code.organization_code=%s AND code.is_active=1
              AND c.is_active=1
            ORDER BY c.id
            """,
            (code,),
        )
        matches = await cur.fetchall()
        if len(matches) == 1:
            return QmfOrganizationResolution(
                community_id=int(matches[0][0]),
                community_name=str(matches[0][1] or "").strip(),
                organization_code=code,
                state="matched_code",
            )
        if len(matches) > 1:
            return QmfOrganizationResolution(
                community_id=None,
                community_name="",
                organization_code=code,
                state="ambiguous_code",
                reason="同一组织编码对应多个社区",
            )

    if source_key:
        matches = []
        for item in communities.values():
            labels = [item["name"], *item["aliases"]]
            if any(normalize_community_label(label) == source_key for label in labels):
                matches.append(item)
        if len(matches) == 1:
            return QmfOrganizationResolution(
                community_id=int(matches[0]["id"]),
                community_name=str(matches[0]["name"]),
                organization_code=code,
                state="matched_name" if not code else "matched_name_without_code",
                reason="组织编码未配置，使用来源社区名/别名匹配",
            )
        if len(matches) > 1:
            return QmfOrganizationResolution(
                community_id=None,
                community_name="",
                organization_code=code,
                state="ambiguous_name",
                reason="来源社区名对应多个社区",
            )

    return QmfOrganizationResolution(
        community_id=None,
        community_name="",
        organization_code=code,
        state="not_found",
        reason="组织编码和来源社区名均无法匹配",
    )


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
    code = normalize_qmf_community_code(item["qmf_community_code"])
    if not valid_qmf_community_code(code):
        raise ValueError("community_code_missing")
    return QmfCommunity(id=item["id"], name=item["name"], qmf_community_code=code)
