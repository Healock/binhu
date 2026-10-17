"""Conservative physical-house identity across upstream household numbers.

An upstream number identifies a registration, not necessarily a physical house.
Only exact addresses with a dated cancellation/re-registration chain may reuse
an identity. Ambiguous shared addresses require human review.
"""
from services.registry_import import _source_datetime, normalize_household_status, normalize_text
from services.visit_import import normalize_address


def current_household_snapshot(rows: list[dict]) -> dict | None:
    if len(rows) < 2:
        return None
    identities = {(row.get("community_id"), normalize_address(row.get("address"))) for row in rows}
    if len(identities) != 1 or not next(iter(identities))[0] or not next(iter(identities))[1]:
        return None
    if any(not normalize_text(row.get("house_no")) for row in rows):
        return None
    times = [_source_datetime(row.get("updated_at")) for row in rows]
    if not all(times) or len(set(times)) != len(times):
        return None
    live = [row for row in rows if normalize_household_status(row.get("household_status")) == "未注销"]
    if len(live) != 1:
        return None
    current = live[0]
    if any(row is not current and normalize_household_status(row.get("household_status")) != "已注销" for row in rows):
        return None
    return current if _source_datetime(current.get("updated_at")) == max(times) else None


def property_snapshot(row) -> dict:
    """The shared 12-column import identity projection."""
    return {
        "id": int(row[0]), "community_id": row[1], "address": row[3],
        "house_no": row[7], "updated_at": row[8] if len(row) > 8 else None,
        "household_status": row[11] if len(row) > 11 else None,
    }
