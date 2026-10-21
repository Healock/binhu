"""Safe, source-only comparison for responsibility-notice previews."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from services.registry_certificate_source import certificate_content_hash, certificate_source_ref, legacy_certificate_source_ref
from services.registry_certificate_status import certificate_status_summary
from services.registry_import import normalize_address, normalize_community, normalize_text
from services.registry_property_canonical import load_property_identity_graph


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


def _canonicalize_comparison_inputs(
    properties: Iterable[dict[str, Any]],
    existing: Iterable[dict[str, Any]],
    identity_graph,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project read-only certificate comparisons onto current property IDs."""
    properties_by_identity: dict[tuple[int, str, str], dict[str, Any]] = {}
    for source in properties:
        item = dict(source)
        original_id = int(item["id"])
        canonical_id = identity_graph.resolve(original_id).canonical_id
        item["id"] = canonical_id
        key = (
            canonical_id,
            str(item.get("normalized_address") or ""),
            normalize_community(item.get("community_name") or ""),
        )
        previous = properties_by_identity.get(key)
        if previous is None or original_id == canonical_id:
            properties_by_identity[key] = item

    projected_existing: list[dict[str, Any]] = []
    for source in existing:
        item = dict(source)
        original_id = int(item.get("property_id") or 0)
        item["property_id"] = identity_graph.resolve(original_id).canonical_id if original_id else 0
        # Keep every projected row.  Collapsing by source_ref can hide two
        # current notices for one house, which must remain a review condition.
        projected_existing.append(item)
    return list(properties_by_identity.values()), projected_existing


def compare_certificate_snapshot(
    incoming_rows: Iterable[dict[str, Any]],
    existing_rows: Iterable[dict[str, Any]],
    property_rows: Iterable[dict[str, Any]],
    *,
    pending_issue_count: int = 0,
    conflict_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Compare one complete source snapshot with the current local notices.

    This function only returns counts and status codes. It never returns or
    persists source payloads, addresses, names, identity numbers, or images.
    """
    incoming = list(incoming_rows)
    existing = list(existing_rows)
    property_rows = list(property_rows)
    properties = {
        (normalize_address(row.get("normalized_address") or row.get("address") or ""),
         normalize_community(row.get("community_name") or row.get("community") or "")): row
        for row in property_rows
    }
    properties_by_id = {int(row["id"]): row for row in property_rows}
    conflicted_ids = {
        int(prop["id"])
        for pair, prop in properties.items()
        if pair in (conflict_keys or set())
    }
    existing_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    existing_by_derived: dict[str, list[dict[str, Any]]] = defaultdict(list)
    existing_by_property: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in existing:
        item = dict(row)
        item["payload"] = _payload(item.get("payload"))
        ref = normalize_text(item.get("source_ref"))
        if ref:
            existing_by_ref[ref].append(item)
        property_id = int(item.get("property_id") or 0)
        if property_id:
            existing_by_property[property_id].append(item)
        derived = certificate_source_ref(item["payload"])
        existing_by_derived[derived].append(item)
        legacy = legacy_certificate_source_ref(item["payload"])
        if legacy != derived:
            existing_by_derived[legacy].append(item)

    actions = {"added": 0, "updated": 0, "unchanged": 0, "pending_review": 0}
    actions_by_property: dict[int, list[str]] = defaultdict(list)
    issue_breakdown = {
        "duplicate": 0,
        "content_conflict": 0,
        "property_not_found": 0,
        "non_rental_property": 0,
        "community_unresolved": 0,
    }
    predicted_by_property: dict[int, list[dict[str, Any]]] = defaultdict(list)
    incoming_ref_counts: dict[str, int] = defaultdict(int)
    for row in incoming:
        incoming_ref_counts[normalize_text(row.get("source_ref")) or certificate_source_ref(row)] += 1

    for row in incoming:
        ref = normalize_text(row.get("source_ref")) or certificate_source_ref(row)
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
        property_existing = existing_by_property.get(int(prop["id"]), [])
        ref_matches = existing_by_ref.get(ref, [])
        derived_matches = existing_by_derived.get(ref, [])
        identity_matches: list[dict[str, Any]] = []
        for item in [*ref_matches, *derived_matches]:
            if not any(item is matched for matched in identity_matches):
                identity_matches.append(item)
        if len(property_existing) > 1 or len(identity_matches) > 1 or incoming_ref_counts[ref] > 1 or any(
            int(item.get("property_id") or 0) != int(prop["id"]) for item in identity_matches
        ):
            actions["pending_review"] += 1
            issue_breakdown["duplicate"] += 1
            predicted_by_property[int(prop["id"])].append({
                **row, "housing_type": prop.get("housing_type"), "_conflict": True,
            })
            continue
        existing_item = identity_matches[0] if identity_matches else None
        if existing_item is None and len(property_existing) == 1:
            existing_item = property_existing[0]
        if existing_item is None:
            actions["added"] += 1
            actions_by_property[int(prop["id"])].append("added")
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
                actions_by_property[int(prop["id"])].append("unchanged")
            else:
                actions["updated"] += 1
                actions_by_property[int(prop["id"])].append("updated")
        predicted_by_property[int(prop["id"])].append({**row, "housing_type": prop.get("housing_type")})

    for property_id, kinds in actions_by_property.items():
        if len(kinds) <= 1:
            continue
        for kind in kinds:
            actions[kind] -= 1
        actions["pending_review"] += len(kinds)
        issue_breakdown["duplicate"] += 1
        for item in predicted_by_property[property_id]:
            item["_conflict"] = True

    incoming_properties = set(predicted_by_property)
    existing_properties = set(existing_by_property)
    missing = len(existing_properties - incoming_properties)
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
        new = predicted_by_property.get(property_id, [])
        prop = properties_by_id.get(property_id, {})
        housing_type = str(prop.get("housing_type") or (old[0].get("housing_type") if old else ""))
        old_status = _status(old[0], housing_type=housing_type, count=len(old)) if old else str(
            certificate_status_summary(housing_type=housing_type, certificate_count=0, source_ready=True)["certificate_status"]
        )
        if property_id in conflicted_ids or len(old) > 1 or len(new) > 1 or any(item.get("_conflict") for item in new):
            new_status = "multiple_or_conflict"
        elif new:
            new_status = _status(new[0], housing_type=housing_type)
        else:
            new_status = str(certificate_status_summary(
                housing_type=housing_type, certificate_count=0, source_ready=True,
            )["certificate_status"])
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
    rows = list(rows)
    incoming = list(classified["normal_rows"] if "normal_rows" in classified else rows)
    properties: list[dict[str, Any]] = []
    async with conn.cursor() as cur:
        identity_graph = await load_property_identity_graph(cur)
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
            "FROM registry_property_certificates WHERE source_type='certificate' AND source_missing_since IS NULL"
        )
        existing = []
        for row in await cur.fetchall():
            existing.append({
                "property_id": identity_graph.resolve(int(row[0])).canonical_id,
                "source_ref": str(row[1] or ""), "source_content_hash": str(row[2] or ""),
                "actual_renter_name": str(row[3] or ""), "actual_renter_identity_number": str(row[4] or ""),
                "landlord_name": str(row[5] or ""), "signed_status": str(row[6] or ""), "sign_type": str(row[7] or ""),
                "payload": _payload(row[8]),
            })
        # Use each current property as authoritative while keeping exact
        # address/community aliases, and project certificates off retired IDs.
        properties, existing = _canonicalize_comparison_inputs(properties, existing, identity_graph)
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
    conflict_keys = {
        (normalize_address(issue["payload"].get("address")), normalize_community(issue["payload"].get("community")))
        for issue in classified.get("issues", [])
    }
    result = compare_certificate_snapshot(
        incoming, existing, properties,
        pending_issue_count=int(classified.get("problem_row_count") or 0),
        conflict_keys=conflict_keys,
    )
    result["incoming_total"] = len(list(rows))
    result["issue_breakdown"]["duplicate"] += issue_breakdown["duplicate"]
    result["issue_breakdown"]["content_conflict"] += issue_breakdown["content_conflict"]
    result["pending_review"] = max(
        result["pending_review"],
        int(classified.get("problem_row_count") or 0),
    )
    return result
