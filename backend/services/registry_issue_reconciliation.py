"""Reconcile registry import issues after community aliases change."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from services.registry_import import (
    ISSUE_HOUSEHOLD_COMMUNITY_UNRESOLVED,
    normalize_address,
    normalize_community,
    normalize_household_status,
    normalize_text,
    _source_datetime as source_datetime,
)
from services.registry_property_identity import current_household_snapshot, property_snapshot

logger = logging.getLogger(__name__)


def organization_pool_key(platform_domain_active: bool) -> str:
    """Select the committed organization database for the current migration phase."""
    return "platform" if platform_domain_active else "online_data"


def community_labels(formal_name: str, aliases: Iterable[str]) -> set[str]:
    """Return exact current labels that belong to one formal community."""
    return {
        normalize_community(value)
        for value in (formal_name, *aliases)
        if normalize_community(value)
    }


def issue_matches_community(payload_json: Any, labels: set[str]) -> bool:
    """Check an unresolved issue using only its stored source community."""
    if not labels:
        return False
    if isinstance(payload_json, str):
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            return False
    else:
        payload = payload_json
    if not isinstance(payload, dict):
        return False
    source_community = normalize_community(
        payload.get("community")
        or payload.get("community_name")
        or payload.get("社区名称")
        or payload.get("sssq")
    )
    return bool(source_community and source_community in labels)


def source_community(payload_json: Any) -> str:
    if isinstance(payload_json, str):
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            return ""
    else:
        payload = payload_json
    if not isinstance(payload, dict):
        return ""
    return normalize_community(
        payload.get("community")
        or payload.get("community_name")
        or payload.get("社区名称")
        or payload.get("sssq")
    )


def unique_community_matches(
    payload_json: Any,
    labels_by_community: dict[int, tuple[str, set[str]]],
) -> tuple[int, str] | None:
    """Return the only active community matching a source label."""
    source = source_community(payload_json)
    if not source:
        return None
    matches = [
        (community_id, formal_name)
        for community_id, (formal_name, labels) in labels_by_community.items()
        if source in labels
    ]
    return matches[0] if len(matches) == 1 else None


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


async def _reconcile_source_row(cur, *, batch_id: int, source_ref: str,
                                payload: dict[str, Any], community_id: int,
                                community_name: str, reviewer_id: int | None,
                                apply: bool = True) -> bool:
    """Link one newly-resolvable source row using the import identity rules."""
    address = normalize_text(payload.get("address"))
    normalized = normalize_address(address)
    house_no = normalize_text(payload.get("house_no"))
    housing_type = normalize_text(payload.get("housing_type"))
    if not normalized or not housing_type:
        return False

    await cur.execute(
        "SELECT id, community_id, street, natural_address, building, room, "
        "normalized_address, source_house_no, source_updated_at, housing_type, residence_type, household_status FROM registry_properties "
        "WHERE community_id=%s AND (normalized_address=%s OR (%s<>'' AND source_house_no=%s)) "
        "FOR UPDATE",
        (community_id, normalized, house_no, house_no),
    )
    existing = await cur.fetchall()
    address_matches = [row for row in existing if str(row[6] or "") == normalized]
    household_matches = [row for row in existing if house_no and str(row[7] or "") == house_no]
    if len(household_matches) > 1 or (not household_matches and len(address_matches) > 1):
        return False
    legacy_matches = [row for row in address_matches if not normalize_text(row[7])]
    target = household_matches[0] if len(household_matches) == 1 else (
        legacy_matches[0] if house_no and len(legacy_matches) == 1 else
        address_matches[0] if not house_no and len(address_matches) == 1 else None
    )
    if household_matches and str(household_matches[0][6] or "") != normalized:
        return False

    historical_only = False
    if house_no and not household_matches and address_matches and not legacy_matches:
        incoming = {**payload, "community_id": community_id}
        current = current_household_snapshot([*(property_snapshot(row) for row in address_matches), incoming])
        if len(address_matches) != 1 or current is None:
            return False
        target = address_matches[0]
        historical_only = current is not incoming

    status = normalize_household_status(payload.get("household_status"))
    if target:
        property_id = int(target[0])
        if historical_only:
            if not apply:
                return True
            await cur.execute(
                "UPDATE registry_source_records SET entity_id=%s WHERE batch_id=%s "
                "AND source_ref=%s AND entity_type='household_property' AND entity_id IS NULL",
                (property_id, batch_id, source_ref),
            )
            return cur.rowcount > 0
        previous_time = source_datetime(target[8]) if len(target) > 8 else None
        incoming_time = source_datetime(payload.get("updated_at"))
        if previous_time and (not incoming_time or incoming_time < previous_time):
            return False
        if previous_time and incoming_time == previous_time and (
            normalize_text(target[9]) != housing_type
            or normalize_text(target[10]) != normalize_text(payload.get("residence_type"))
            or normalize_household_status(target[11]) != status
        ):
            return False
        await cur.execute(
            "SELECT 1 FROM registry_source_records WHERE entity_type='household_property' "
            "AND entity_id=%s AND batch_id>%s LIMIT 1",
            (property_id, batch_id),
        )
        if await cur.fetchone():
            return False
        if not apply:
            return True
        await cur.execute(
            "UPDATE registry_properties SET community_id=%s, community_name_snapshot=%s, "
            "natural_address=%s, housing_type=%s, residence_type=%s, source_house_no=%s, "
            "source_updated_at=COALESCE(%s,source_updated_at), source_type='household', source_ref=%s, "
            "current_version=current_version+IF(%s IS NOT NULL AND NOT(household_status <=> %s),1,0), "
            "household_status=COALESCE(%s,household_status), updated_by=%s WHERE id=%s",
            (community_id, community_name, address, housing_type,
             normalize_text(payload.get("residence_type")), house_no or target[7],
             source_datetime(payload.get("updated_at")), source_ref,
             status, status, status, reviewer_id, property_id),
        )
        await cur.execute(
            "SELECT 1 FROM registry_property_address_versions WHERE property_id=%s LIMIT 1",
            (property_id,),
        )
        if not await cur.fetchone():
            await cur.execute(
                "INSERT INTO registry_property_address_versions "
                "(property_id, version_no, street, natural_address, building, room, "
                "normalized_address, effective_from, source_type, source_ref, "
                "change_reason, changed_by) VALUES (%s,1,'',%s,'','',%s,UTC_TIMESTAMP(),"
                "'household',%s,'社区别名重算补齐初始地址',%s)",
                (property_id, address, normalized, source_ref, reviewer_id),
            )
    else:
        if not apply:
            return True
        await cur.execute(
            "INSERT INTO registry_properties (street, community_id, community_name_snapshot, "
            "natural_address, building, room, housing_type, residence_type, source_house_no, "
            "source_updated_at, source_type, source_ref, normalized_address, household_status, "
            "created_by, updated_by) VALUES ('',%s,%s,%s,'','',%s,%s,%s,%s,'household',%s,%s,%s,%s,%s)",
            (community_id, community_name, address, housing_type,
             normalize_text(payload.get("residence_type")), house_no,
             source_datetime(payload.get("updated_at")), source_ref, normalized,
             status, reviewer_id, reviewer_id),
        )
        property_id = int(cur.lastrowid)
        await cur.execute(
            "INSERT INTO registry_property_address_versions "
            "(property_id, version_no, street, natural_address, building, room, "
            "normalized_address, effective_from, source_type, source_ref, "
            "change_reason, changed_by) VALUES (%s,1,'',%s,'','',%s,UTC_TIMESTAMP(),"
            "'household',%s,'社区别名重算新增档案',%s)",
            (property_id, address, normalized, source_ref, reviewer_id),
        )
    await cur.execute(
        "UPDATE registry_source_records SET entity_id=%s WHERE batch_id=%s "
        "AND source_ref=%s AND entity_type='household_property' AND entity_id IS NULL",
        (property_id, batch_id, source_ref),
    )
    return cur.rowcount > 0


async def reconcile_household_community_issues(
    *,
    formal_name: str,
    aliases: Iterable[str],
    reviewed_by: int | None,
) -> int:
    """Resolve pending community issues that now match one current label set.

    The registry database is separate from the platform database where aliases
    are edited, so this runs after the platform transaction commits. It is
    idempotent and never changes unresolved, duplicate, or certificate issues.
    """
    labels = community_labels(formal_name, aliases)
    if not labels:
        return 0
    # Import lazily: database.py imports the service package while bootstrapping
    # the domain pools, so a module-level import would create a cycle.
    from database import db_manager

    from config import settings

    platform_pool = db_manager.get_pool(
        organization_pool_key(bool(settings.PLATFORM_DOMAIN_ACTIVE))
    )
    registry_pool = db_manager.get_pool("registry")
    platform_conn = await platform_pool.acquire()
    conn = await registry_pool.acquire()
    resolved = 0
    try:
        # Re-read the committed directory so an alias is only auto-resolved
        # when it belongs to exactly one active community.
        async with platform_conn.cursor() as platform_cur:
            await platform_cur.execute("SELECT id, name FROM _communities WHERE is_active=1")
            labels_by_community = {
                int(row[0]): (str(row[1] or "").strip(), community_labels(str(row[1] or ""), ()))
                for row in await platform_cur.fetchall()
            }
            await platform_cur.execute("SELECT community_id, alias FROM _community_aliases")
            for community_id, alias in await platform_cur.fetchall():
                item = labels_by_community.get(int(community_id))
                normalized = normalize_community(alias)
                if item and normalized:
                    item[1].add(normalized)
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, batch_id, source_ref, payload_json FROM registry_import_issues "
                "WHERE status='pending' AND source_type='household' "
                "AND issue_type=%s",
                (ISSUE_HOUSEHOLD_COMMUNITY_UNRESOLVED,),
            )
            candidates = await cur.fetchall()
            linked_by_batch: dict[int, int] = {}
            for issue_id, batch_id, source_ref, payload_json in candidates:
                match = unique_community_matches(payload_json, labels_by_community)
                if not match or normalize_community(match[1]) not in labels:
                    continue
                await cur.execute(
                    "SELECT id, entity_id, payload_json FROM registry_source_records "
                    "WHERE batch_id=%s AND source_ref=%s AND entity_type='household_property' "
                    "ORDER BY id LIMIT 1 FOR UPDATE",
                    (batch_id, source_ref),
                )
                source = await cur.fetchone()
                if not source:
                    continue
                source_payload = _payload(source[2])
                if source[1] is None and not await _reconcile_source_row(
                    cur, batch_id=int(batch_id), source_ref=str(source_ref),
                    payload=source_payload, community_id=match[0],
                    community_name=match[1], reviewer_id=reviewed_by,
                ):
                    continue
                await cur.execute(
                    "UPDATE registry_import_issues SET status='resolved', "
                    "review_note=%s, reviewed_by=%s, reviewed_at=UTC_TIMESTAMP() "
                    "WHERE id=%s AND status='pending'",
                    (
                        f"社区别名重算后自动归入：{formal_name}",
                        reviewed_by,
                        issue_id,
                    ),
                )
                resolved += cur.rowcount
                if cur.rowcount:
                    linked_by_batch[int(batch_id)] = linked_by_batch.get(int(batch_id), 0) + 1
            for batch_id, linked_count in linked_by_batch.items():
                await cur.execute(
                    "SELECT COUNT(*) FROM registry_import_issues WHERE batch_id=%s AND status='pending'",
                    (batch_id,),
                )
                pending = int((await cur.fetchone())[0])
                await cur.execute(
                    "UPDATE registry_source_batches SET status=%s, imported_count=imported_count+%s "
                    "WHERE id=%s AND source_type='household'",
                    ("partially_imported" if pending else "imported", linked_count, batch_id),
                )
        await conn.commit()
        return resolved
    except Exception:
        await conn.rollback()
        raise
    finally:
        registry_pool.release(conn)
        platform_pool.release(platform_conn)


async def reconcile_household_community_issues_safely(**kwargs) -> int:
    """Best-effort wrapper used after a committed community update."""
    try:
        return await reconcile_household_community_issues(**kwargs)
    except Exception:
        logger.exception("registry community issue reconciliation failed")
        return -1
