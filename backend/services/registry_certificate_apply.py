"""Transactional application of validated responsibility-notice previews."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from services.registry_certificate_source import (
    certificate_content_hash,
    certificate_source_ref,
)
from services.registry_import import (
    ISSUE_CERTIFICATE_NON_RENTAL,
    normalize_address,
    normalize_community,
    normalize_text,
)


WRITE_CHUNK = 500
RENTAL_HOUSING_TYPES = {"个人出租", "单位出租"}


def certificate_write_action(
    existing: tuple | None,
    *,
    source_ref: str,
    content_hash: str,
    property_id: int,
) -> str:
    if not existing:
        return "insert"
    same_ref = str(existing[1]) == source_ref
    same_property = int(existing[2]) == property_id
    same_content = str(existing[3] or "") == content_hash
    return "unchanged" if same_ref and same_property and same_content else "update"


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(default)) else default
        except (TypeError, ValueError):
            return default
    return default


def _source_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = normalize_text(value)
    if not text:
        return None
    normalized = text.replace("/", "-").replace("T", " ").removesuffix("Z")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


async def _canonical_community(cur, snapshot: str) -> tuple[int | None, str]:
    name = normalize_community(snapshot)
    if not name:
        return None, ""
    await cur.execute(
        "SELECT community.id, community.name FROM OnlineData._communities community "
        "WHERE community.name=%s AND community.is_active=1 "
        "UNION ALL "
        "SELECT community.id, community.name FROM OnlineData._community_aliases alias "
        "JOIN OnlineData._communities community ON community.id=alias.community_id "
        "WHERE alias.alias=%s AND community.is_active=1 LIMIT 1",
        (name, name),
    )
    row = await cur.fetchone()
    return (int(row[0]), str(row[1]).strip()) if row else (None, name)


async def _executemany(cur, sql: str, rows: list[tuple]) -> None:
    for offset in range(0, len(rows), WRITE_CHUNK):
        await cur.executemany(sql, rows[offset:offset + WRITE_CHUNK])


async def apply_certificate_batch(
    conn,
    batch_id: int,
    actor_id: int | None,
) -> dict[str, Any]:
    """Apply one complete preview, updating changed notices instead of ignoring them."""
    await conn.begin()
    inserted = 0
    updated = 0
    unchanged = 0
    skipped = 0
    pending_issue_count = 0
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM registry_source_batches "
                "WHERE id=%s AND source_type='certificate' FOR UPDATE",
                (batch_id,),
            )
            batch = await cur.fetchone()
            if not batch:
                raise LookupError("告知书导入批次不存在")
            if str(batch[0]) in {"imported", "partially_imported"}:
                await cur.execute(
                    "SELECT COUNT(*) FROM registry_source_records "
                    "WHERE batch_id=%s AND entity_type='property_certificate' AND entity_id IS NOT NULL",
                    (batch_id,),
                )
                seen_count = int((await cur.fetchone())[0])
                await cur.execute(
                    "UPDATE registry_property_certificates certificate "
                    "JOIN registry_source_records source ON source.entity_id=certificate.id "
                    "SET certificate.source_last_seen_at=UTC_TIMESTAMP(),"
                    "certificate.source_missing_since=NULL "
                    "WHERE source.batch_id=%s AND source.entity_type='property_certificate'",
                    (batch_id,),
                )
                await conn.commit()
                return {
                    "batch_id": batch_id,
                    "status": str(batch[0]),
                    "inserted_count": 0,
                    "updated_count": 0,
                    "unchanged_count": seen_count,
                    "skipped_count": 0,
                    "pending_issue_count": 0,
                    "idempotent": True,
                }

            await cur.execute(
                "SELECT id,source_ref,payload_json FROM registry_source_records "
                "WHERE batch_id=%s AND entity_type='property_certificate' ORDER BY id",
                (batch_id,),
            )
            records = list(await cur.fetchall())
            await cur.execute(
                "SELECT source_ref FROM registry_import_issues "
                "WHERE batch_id=%s AND status='pending'",
                (batch_id,),
            )
            blocked_refs = {str(row[0]) for row in await cur.fetchall()}

            community_cache: dict[str, tuple[int | None, str]] = {}
            candidates: list[dict[str, Any]] = []
            for record_id, stored_ref, payload_json in records:
                payload = _json(payload_json, {})
                stable_ref = str(payload.get("source_ref") or certificate_source_ref(payload))
                if str(stored_ref) in blocked_refs or stable_ref in blocked_refs:
                    skipped += 1
                    continue
                address = normalize_text(payload.get("address") or payload.get("dz"))
                normalized = normalize_address(address)
                if not normalized:
                    skipped += 1
                    continue
                community_name = normalize_community(payload.get("community") or payload.get("sssq"))
                if community_name not in community_cache:
                    community_cache[community_name] = await _canonical_community(cur, community_name)
                community_id, canonical_name = community_cache[community_name]
                candidates.append({
                    "record_id": int(record_id),
                    "source_ref": stable_ref,
                    "payload": payload,
                    "content_hash": str(
                        payload.get("source_content_hash") or certificate_content_hash(payload)
                    ),
                    "community_id": community_id,
                    "community_name": canonical_name,
                    "normalized": normalized,
                    "address": address,
                })

            property_cache: dict[tuple[str, int | None], tuple[int, str]] = {}
            keys = sorted({str(item["normalized"]) for item in candidates})
            for offset in range(0, len(keys), WRITE_CHUNK):
                chunk = keys[offset:offset + WRITE_CHUNK]
                placeholders = ",".join(["%s"] * len(chunk))
                await cur.execute(
                    "SELECT id,community_id,normalized_address,housing_type "
                    "FROM registry_properties "
                    f"WHERE normalized_address IN ({placeholders}) FOR UPDATE",
                    tuple(chunk),
                )
                for property_id, community_id, normalized, housing_type in await cur.fetchall():
                    property_cache[(str(normalized), community_id)] = (
                        int(property_id), str(housing_type or "")
                    )

            await cur.execute(
                "SELECT id,source_ref,property_id,source_content_hash,payload_json "
                "FROM registry_property_certificates WHERE source_type='certificate' FOR UPDATE"
            )
            existing_rows = list(await cur.fetchall())
            existing_by_ref = {str(row[1]): row for row in existing_rows}
            derived: dict[str, list[tuple]] = defaultdict(list)
            for row in existing_rows:
                old_payload = _json(row[4], {})
                if old_payload:
                    derived[certificate_source_ref(old_payload)].append(row)
            existing_by_derived = {
                ref: rows[0] for ref, rows in derived.items() if len(rows) == 1
            }

            inserts: list[tuple] = []
            updates: list[tuple] = []
            touches: list[tuple] = []
            source_links: list[tuple] = []
            non_rental_issues: list[tuple] = []
            claimed_existing_ids: set[int] = set()

            for item in candidates:
                payload = item["payload"]
                property_row = property_cache.get((item["normalized"], item["community_id"]))
                if not property_row or property_row[1] not in RENTAL_HOUSING_TYPES:
                    non_rental_issues.append((
                        batch_id,
                        ISSUE_CERTIFICATE_NON_RENTAL,
                        "certificate",
                        item["source_ref"],
                        item["normalized"],
                        json.dumps(payload, ensure_ascii=False, default=str),
                        "告知书地址未匹配到个人出租/单位出租房屋档案",
                    ))
                    skipped += 1
                    continue

                property_id = property_row[0]
                values = (
                    property_id,
                    item["source_ref"],
                    item["content_hash"],
                    str(payload.get("source_row") or ""),
                    item["community_name"],
                    item["address"],
                    str(payload.get("czrxm") or payload.get("landlord_name") or ""),
                    str(payload.get("czrzjhm") or payload.get("landlord_identity_number") or ""),
                    str(payload.get("sjczrxm") or payload.get("actual_renter_name") or ""),
                    str(payload.get("sjczrzjhm") or payload.get("actual_renter_identity_number") or ""),
                    str(payload.get("isSign") or payload.get("signed_status") or ""),
                    str(payload.get("signType") or payload.get("sign_type") or ""),
                    _source_datetime(payload.get("signTime") or payload.get("sign_time")),
                    str(payload.get("signurl") or payload.get("document_ref") or ""),
                    json.dumps(payload, ensure_ascii=False, default=str),
                )
                existing = existing_by_ref.get(item["source_ref"]) or existing_by_derived.get(item["source_ref"])
                if existing and int(existing[0]) not in claimed_existing_ids:
                    certificate_id = int(existing[0])
                    claimed_existing_ids.add(certificate_id)
                    action = certificate_write_action(
                        existing,
                        source_ref=item["source_ref"],
                        content_hash=item["content_hash"],
                        property_id=property_id,
                    )
                    if action == "unchanged":
                        touches.append((str(payload.get("source_row") or ""), certificate_id))
                        unchanged += 1
                    else:
                        updates.append((*values, certificate_id))
                        updated += 1
                    source_links.append((certificate_id, item["record_id"]))
                else:
                    inserts.append((*values, actor_id))

            if non_rental_issues:
                await _executemany(
                    cur,
                    "INSERT INTO registry_import_issues "
                    "(batch_id,issue_type,source_type,source_ref,entity_key,payload_json,reason) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    non_rental_issues,
                )
            if updates:
                await _executemany(
                    cur,
                    "UPDATE registry_property_certificates SET "
                    "property_id=%s,source_ref=%s,source_content_hash=%s,source_row=%s,"
                    "community_snapshot=%s,address_snapshot=%s,landlord_name=%s,"
                    "landlord_identity_number=%s,actual_renter_name=%s,"
                    "actual_renter_identity_number=%s,signed_status=%s,sign_type=%s,"
                    "sign_time=%s,document_ref=%s,payload_json=%s,"
                    "source_first_seen_at=COALESCE(source_first_seen_at,created_at),"
                    "source_last_seen_at=UTC_TIMESTAMP(),source_missing_since=NULL WHERE id=%s",
                    updates,
                )
            if touches:
                await _executemany(
                    cur,
                    "UPDATE registry_property_certificates SET source_row=%s,"
                    "source_first_seen_at=COALESCE(source_first_seen_at,created_at),"
                    "source_last_seen_at=UTC_TIMESTAMP(),source_missing_since=NULL WHERE id=%s",
                    touches,
                )
            if inserts:
                await _executemany(
                    cur,
                    "INSERT INTO registry_property_certificates "
                    "(property_id,source_type,source_ref,source_content_hash,source_row,"
                    "community_snapshot,address_snapshot,landlord_name,landlord_identity_number,"
                    "actual_renter_name,actual_renter_identity_number,signed_status,sign_type,"
                    "sign_time,document_ref,payload_json,source_first_seen_at,source_last_seen_at,created_by) "
                    "VALUES (%s,'certificate',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "UTC_TIMESTAMP(),UTC_TIMESTAMP(),%s)",
                    inserts,
                )
                inserted = len(inserts)

            unresolved_links = [item for item in candidates if item["record_id"] not in {link[1] for link in source_links}]
            if unresolved_links:
                refs = sorted({str(item["source_ref"]) for item in unresolved_links})
                for offset in range(0, len(refs), WRITE_CHUNK):
                    chunk = refs[offset:offset + WRITE_CHUNK]
                    placeholders = ",".join(["%s"] * len(chunk))
                    await cur.execute(
                        "SELECT id,source_ref FROM registry_property_certificates "
                        f"WHERE source_type='certificate' AND source_ref IN ({placeholders})",
                        tuple(chunk),
                    )
                    ids = {str(ref): int(certificate_id) for certificate_id, ref in await cur.fetchall()}
                    source_links.extend(
                        (ids[item["source_ref"]], item["record_id"])
                        for item in unresolved_links
                        if item["source_ref"] in ids
                    )
            if source_links:
                await _executemany(
                    cur,
                    "UPDATE registry_source_records SET entity_id=%s WHERE id=%s",
                    source_links,
                )

            await cur.execute(
                "SELECT COUNT(*) FROM registry_import_issues "
                "WHERE batch_id=%s AND status='pending'",
                (batch_id,),
            )
            pending_issue_count = int((await cur.fetchone())[0])
            status = "partially_imported" if pending_issue_count else "imported"
            processed = inserted + updated + unchanged
            await cur.execute(
                "UPDATE registry_source_batches SET status=%s,imported_count=%s WHERE id=%s",
                (status, processed, batch_id),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    return {
        "batch_id": batch_id,
        "status": "partially_imported" if pending_issue_count else "imported",
        "inserted_count": inserted,
        "updated_count": updated,
        "unchanged_count": unchanged,
        "imported_count": inserted + updated + unchanged,
        "skipped_count": skipped,
        "pending_issue_count": pending_issue_count,
        "idempotent": False,
    }
