"""Safe, source-only comparison for responsibility-notice previews."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from services.registry_certificate_source import certificate_content_hash, certificate_source_ref, legacy_certificate_source_ref
from services.registry_certificate_status import certificate_status_summary
from services.registry_import import normalize_address, normalize_community, normalize_text


STATUS_KEYS = (
    "normal_signed",
    "not_required",
    "not_uploaded",
    "renter_needs_correction",
    "actual_renter_missing",
    "multiple_or_conflict",
)


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json

        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _status(value: dict[str, Any], *, housing_type: str, count: int = 1) -> str:
    return str(certificate_status_summary(
        housing_type=housing_type,
        certificate_count=count,
        landlord_name=value.get("landlord_name") or value.get("czrxm"),
        actual_renter_name=value.get("actual_renter_name") or value.get("sjczrxm"),
        signed_status=value.get("signed_status") or value.get("isSign"),
        sign_type=value.get("sign_type") or value.get("signType"),
    )["certificate_status"])


def _empty_status() -> dict[str, dict[str, int]]:
    return {key: {"total": 0, "entered": 0, "exited": 0} for key in STATUS_KEYS}


def compare_certificate_snapshot(
    incoming_rows: Iterable[dict[str, Any]],
    existing_rows: Iterable[dict[str, Any]],
    property_rows: Iterable[dict[str, Any]],
    *,
    pending_issue_count: int = 0,
) -> dict[str, Any]:
    """Compare one complete source snapshot with the current local notices.

    This function only returns counts and status codes. It never returns or
    persists source payloads, addresses, names, identity numbers, or images.
    """
    incoming = list(incoming_rows)
    existing = list(existing_rows)
    properties = {
        (normalize_address(row.get("normalized_address") or row.get("address") or ""),
         normalize_community(row.get("community_name") or row.get("community") or "")): row
        for row in property_rows
    }
    existing_by_ref: dict[str, dict[str, Any]] = {}
    existing_by_derived: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in existing:
        item = dict(row)
        item["payload"] = _payload(item.get("payload"))
        ref = normalize_text(item.get("source_ref"))
        if ref:
            existing_by_ref[ref] = item
        derived = certificate_source_ref(item["payload"])
        existing_by_derived[derived].append(item)
        legacy = legacy_certificate_source_ref(item["payload"])
        if legacy != derived:
            existing_by_derived[legacy].append(item)

    seen_refs: set[str] = set()
    actions = {"added": 0, "updated": 0, "unchanged": 0, "pending_review": 0}
    issue_breakdown = {
        "duplicate": 0,
        "content_conflict": 0,
        "property_not_found": 0,
        "non_rental_property": 0,
        "community_unresolved": 0,
    }
    predicted_by_property: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for row in incoming:
        ref = normalize_text(row.get("source_ref")) or certificate_source_ref(row)
        seen_refs.add(ref)
        community = normalize_community(row.get("community") or row.get("sssq"))
        normalized = normalize_address(row.get("address") or row.get("dz"))
        prop = properties.get((normalized, community))
        if not community:
            issue_breakdown["community_unresolved"] += 1
        elif not prop:
            issue_breakdown["property_not_found"] += 1
        elif str(prop.get("housing_type") or "") not in {"个人出租", "单位出租"}:
            issue_breakdown["non_rental_property"] += 1
        if not prop or str(prop.get("housing_type") or "") not in {"个人出租", "单位出租"}:
            actions["pending_review"] += 1
            continue
        existing_item = existing_by_ref.get(ref)
        if existing_item is None and len(existing_by_derived.get(ref, [])) == 1:
            existing_item = existing_by_derived[ref][0]
        if existing_item is None:
            actions["added"] += 1
        else:
            same_property = int(existing_item.get("property_id") or 0) == int(prop.get("id") or 0)
            incoming_hash = certificate_content_hash(row)
            old_payload_hash = certificate_content_hash(existing_item.get("payload") or {})
            same_content = (
                normalize_text(existing_item.get("source_content_hash")) == incoming_hash
                or old_payload_hash == incoming_hash
            )
            if same_property and same_content:
                actions["unchanged"] += 1
            else:
                actions["updated"] += 1
        predicted_by_property[int(prop["id"])].append({**row, "housing_type": prop.get("housing_type")})

    missing = sum(1 for row in existing if normalize_text(row.get("source_ref")) not in seen_refs)
    status_summary = _empty_status()
    old_by_property: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in existing:
        old_by_property[int(row.get("property_id") or 0)].append(row)
    property_ids = (
        set(old_by_property)
        | set(predicted_by_property)
        | {int(row.get("id") or 0) for row in property_rows if int(row.get("id") or 0)}
    )
    for property_id in property_ids:
        old = old_by_property.get(property_id, [])
        new = predicted_by_property.get(property_id, old)
        prop = next((row for row in property_rows if int(row.get("id") or 0) == property_id), {})
        housing_type = str(prop.get("housing_type") or (old[0].get("housing_type") if old else ""))
        old_status = _status(old[0], housing_type=housing_type, count=len(old)) if old else str(
            certificate_status_summary(housing_type=housing_type, certificate_count=0, source_ready=True)["certificate_status"]
        )
        if len(new) > 1:
            new_status = "multiple_or_conflict"
        elif new:
            new_status = _status(new[0], housing_type=housing_type)
        else:
            new_status = old_status
        if old and old_status in status_summary:
            status_summary[old_status]["total"] -= 1
        if new_status in status_summary:
            status_summary[new_status]["total"] += 1
        if old_status != new_status:
            if old and old_status in status_summary:
                status_summary[old_status]["exited"] += 1
            if new_status in status_summary:
                status_summary[new_status]["entered"] += 1

    for key in STATUS_KEYS:
        status_summary[key]["total"] = max(0, status_summary[key]["total"])
    return {
        "compared_at": _now(),
        "existing_total": len(existing),
        "incoming_total": len(incoming),
        "added": actions["added"],
        "updated": actions["updated"],
        "unchanged": actions["unchanged"],
        "missing_from_source": missing,
        "safe_to_apply": actions["added"] + actions["updated"] + actions["unchanged"],
        "pending_review": actions["pending_review"] + int(pending_issue_count or 0),
        "status_summary": status_summary,
        "issue_breakdown": issue_breakdown,
    }


async def load_certificate_comparison(conn, rows: Iterable[dict[str, Any]], classified: dict[str, Any]) -> dict[str, Any]:
    """Load only the local keys needed for a preview comparison."""
    incoming = list(classified.get("normal_rows") or rows)
    properties: list[dict[str, Any]] = []
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT p.id,p.normalized_address,p.housing_type,c.name "
            "FROM registry_properties p LEFT JOIN OnlineData._communities c ON c.id=p.community_id"
        )
        properties.extend(
            {"id": int(row[0]), "normalized_address": str(row[1] or ""), "housing_type": str(row[2] or ""), "community_name": str(row[3] or "")}
            for row in await cur.fetchall()
        )
        await cur.execute(
            "SELECT p.id,p.normalized_address,p.housing_type,a.alias "
            "FROM registry_properties p JOIN OnlineData._community_aliases a ON a.community_id=p.community_id"
        )
        properties.extend(
            {"id": int(row[0]), "normalized_address": str(row[1] or ""), "housing_type": str(row[2] or ""), "community_name": str(row[3] or "")}
            for row in await cur.fetchall()
        )
        await cur.execute(
            "SELECT property_id,source_ref,source_content_hash,actual_renter_name,"
            "actual_renter_identity_number,landlord_name,signed_status,sign_type,payload_json "
            "FROM registry_property_certificates WHERE source_type='certificate'"
        )
        existing = []
        for row in await cur.fetchall():
            existing.append({
                "property_id": int(row[0]), "source_ref": str(row[1] or ""), "source_content_hash": str(row[2] or ""),
                "actual_renter_name": str(row[3] or ""), "actual_renter_identity_number": str(row[4] or ""),
                "landlord_name": str(row[5] or ""), "signed_status": str(row[6] or ""), "sign_type": str(row[7] or ""),
                "payload": _payload(row[8]),
            })
        existing_property_ids = sorted({int(row["property_id"]) for row in existing if int(row.get("property_id") or 0)})
        known_property_ids = {int(row.get("id") or 0) for row in properties}
        missing_property_ids = [item for item in existing_property_ids if item not in known_property_ids]
        if missing_property_ids:
            placeholders = ",".join(["%s"] * len(missing_property_ids))
            await cur.execute(
                "SELECT p.id,p.normalized_address,p.housing_type,c.name "
                "FROM registry_properties p LEFT JOIN OnlineData._communities c ON c.id=p.community_id "
                f"WHERE p.id IN ({placeholders})",
                tuple(missing_property_ids),
            )
            properties.extend(
                {"id": int(row[0]), "normalized_address": str(row[1] or ""), "housing_type": str(row[2] or ""), "community_name": str(row[3] or "")}
                for row in await cur.fetchall()
            )
    issue_breakdown = {
        "duplicate": int(classified.get("duplicate_groups") or 0),
        "content_conflict": int(classified.get("conflict_groups") or 0),
        "property_not_found": 0, "non_rental_property": 0, "community_unresolved": 0,
    }
    result = compare_certificate_snapshot(incoming, existing, properties, pending_issue_count=int(classified.get("problem_row_count") or 0))
    result["incoming_total"] = len(list(rows))
    result["issue_breakdown"]["duplicate"] = issue_breakdown["duplicate"]
    result["issue_breakdown"]["content_conflict"] = issue_breakdown["content_conflict"]
    result["pending_review"] = max(
        result["pending_review"],
        int(classified.get("problem_row_count") or 0),
    )
    return result
