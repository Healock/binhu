"""人员标签与辖区人员档案的安全关联与历史回填。"""

from __future__ import annotations

import re
from typing import Any

import aiomysql


IDENTITY_HMAC_RE = re.compile(r"^[0-9a-f]{64}$")


def valid_identity_hmac(value: object) -> bool:
    return bool(IDENTITY_HMAC_RE.fullmatch(str(value or "").strip().lower()))


def empty_backfill_summary() -> dict[str, int]:
    return {
        "eligible": 0,
        "created_registry_people": 0,
        "reused_registry_people": 0,
        "linked_watch_people": 0,
        "new_registry_phones": 0,
        "skipped_missing_identity": 0,
        "conflicts": 0,
        "skipped": 0,
    }


async def ensure_watch_person_registry_link(
    cur,
    watch_person_id: int,
    *,
    source_type: str = "legacy_watch_migration",
    source_ref: str | None = None,
    actor_id: int | None = None,
) -> dict[str, Any]:
    """Create/reuse the archive person and link one tag person.

    The caller owns the transaction. Existing registry fields are never
    overwritten. A non-null link to a different archive person is treated as
    a conflict and left untouched.
    """
    await cur.execute(
        "SELECT id, name, identity_number, identity_hmac, identity_hmac_version, verification_status, "
        "is_temporary, registry_person_id "
        "FROM watch_people WHERE id=%s FOR UPDATE",
        (watch_person_id,),
    )
    watch = await cur.fetchone()
    if not watch:
        return {"status": "skipped", "reason": "watch_person_missing"}
    _, name, identity_number, identity_hmac, hmac_version, verification_status, is_temporary, current_registry_id = watch
    digest = str(identity_hmac or "").strip().lower()
    if not valid_identity_hmac(digest):
        return {"status": "skipped", "reason": "missing_identity"}

    await cur.execute(
        "SELECT id FROM registry_housing_people WHERE identity_hmac=%s FOR UPDATE",
        (digest,),
    )
    registry_rows = await cur.fetchall()
    if len(registry_rows) > 1:
        return {"status": "conflict", "reason": "duplicate_registry_identity"}
    if current_registry_id is not None:
        current_registry_id = int(current_registry_id)
        await cur.execute(
            "SELECT identity_hmac FROM registry_housing_people WHERE id=%s FOR UPDATE",
            (current_registry_id,),
        )
        linked_row = await cur.fetchone()
        if not linked_row or str(linked_row[0] or "").strip().lower() != digest:
            return {"status": "conflict", "reason": "link_points_to_different_person"}
        if registry_rows and int(registry_rows[0][0]) != current_registry_id:
            return {"status": "conflict", "reason": "link_points_to_different_person"}
        registry_person_id = current_registry_id
        created = False
    elif registry_rows:
        registry_person_id = int(registry_rows[0][0])
        created = False
    else:
        try:
            await cur.execute(
                "INSERT INTO registry_housing_people "
                "(name, identity_number, identity_hmac, identity_hmac_version, is_temporary, verification_status, "
                "source_type, source_ref, created_by, updated_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(name or "")[:100] or "未命名人员",
                    str(identity_number or "")[:50] or None,
                digest,
                int(hmac_version or 1),
                int(bool(is_temporary)),
                str(verification_status or "unverified")[:20],
                    source_type[:30],
                    (source_ref or f"watch_person:{watch_person_id}")[:190],
                    actor_id,
                    actor_id,
                ),
            )
            registry_person_id = int(cur.lastrowid)
            created = True
        except aiomysql.IntegrityError:
            # Another transaction may have inserted the same digest after
            # the initial SELECT. Re-read it without changing its fields.
            await cur.execute(
                "SELECT id FROM registry_housing_people WHERE identity_hmac=%s FOR UPDATE",
                (digest,),
            )
            retry_rows = await cur.fetchall()
            if len(retry_rows) != 1:
                return {"status": "conflict", "reason": "duplicate_registry_identity"}
            registry_person_id = int(retry_rows[0][0])
            created = False

    linked = False
    if current_registry_id is None:
        await cur.execute(
            "UPDATE watch_people SET registry_person_id=%s WHERE id=%s AND registry_person_id IS NULL",
            (registry_person_id, watch_person_id),
        )
        linked = cur.rowcount == 1

    new_phones = 0
    await cur.execute(
        "SELECT phone, phone_hmac, hmac_version, is_primary, verified, valid_from, valid_to "
        "FROM watch_person_phones WHERE person_id=%s AND phone_hmac IS NOT NULL "
        "AND phone_hmac<>'' AND phone<>'' "
        "AND (valid_from IS NULL OR valid_from<=UTC_TIMESTAMP()) "
        "AND (valid_to IS NULL OR valid_to>=UTC_TIMESTAMP())",
        (watch_person_id,),
    )
    for phone, phone_hmac, phone_version, is_primary, verified, valid_from, valid_to in await cur.fetchall():
        await cur.execute(
            "SELECT id FROM registry_person_phones WHERE person_id=%s AND phone_hmac=%s "
            "AND ((valid_from=%s) OR (valid_from IS NULL AND %s IS NULL)) "
            "AND ((valid_to=%s) OR (valid_to IS NULL AND %s IS NULL)) LIMIT 1",
            (registry_person_id, phone_hmac, valid_from, valid_from, valid_to, valid_to),
        )
        if await cur.fetchone():
            continue
        await cur.execute(
            "INSERT INTO registry_person_phones "
            "(person_id, phone, phone_hmac, hmac_version, is_primary, verified, valid_from, valid_to, "
            "source_type, source_ref, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                registry_person_id,
                phone,
                phone_hmac,
                int(phone_version or 1),
                int(bool(is_primary)),
                int(bool(verified)),
                valid_from,
                valid_to,
                source_type[:30],
                (source_ref or f"watch_person:{watch_person_id}")[:190],
                actor_id,
            ),
        )
        new_phones += 1

    return {
        "status": "created" if created else "linked" if linked else "reused",
        "registry_person_id": registry_person_id,
        "created": created,
        "linked": linked,
        "new_phones": new_phones,
    }


async def backfill_watch_people(cur, *, apply: bool, batch_size: int = 500) -> dict[str, int]:
    """Measure or apply the historical active-tag backfill.

    Dry-run mode executes only SELECTs and reports the changes that would be
    made. Apply mode must be called inside a transaction owned by the caller.
    """
    summary = empty_backfill_summary()
    batch_size = max(1, min(int(batch_size), 2000))
    await cur.execute(
        "SELECT id FROM watch_people WHERE status='active' ORDER BY id"
    )
    ids = [int(row[0]) for row in await cur.fetchall()]
    summary["eligible"] = len(ids)
    if apply:
        for start in range(0, len(ids), batch_size):
            for person_id in ids[start : start + batch_size]:
                result = await ensure_watch_person_registry_link(cur, person_id)
                status = result.get("status")
                if status == "created":
                    summary["created_registry_people"] += 1
                elif status in {"linked", "reused"}:
                    summary["reused_registry_people"] += 1
                if result.get("linked"):
                    summary["linked_watch_people"] += 1
                summary["new_registry_phones"] += int(result.get("new_phones") or 0)
                if status == "conflict":
                    summary["conflicts"] += 1
                elif status == "skipped":
                    if result.get("reason") == "missing_identity":
                        summary["skipped_missing_identity"] += 1
                    else:
                        summary["skipped"] += 1
        return summary

    # Dry-run uses the same identity/link rules but never invokes INSERT or
    # UPDATE. Phone counts are calculated against the current archive state.
    for person_id in ids:
        await cur.execute(
            "SELECT name, identity_number, identity_hmac, identity_hmac_version, registry_person_id "
            "FROM watch_people WHERE id=%s",
            (person_id,),
        )
        watch = await cur.fetchone()
        if not watch:
            summary["skipped"] += 1
            continue
        name, identity_number, digest, hmac_version, linked_id = watch
        digest = str(digest or "").strip().lower()
        if not valid_identity_hmac(digest):
            summary["skipped_missing_identity"] += 1
            continue
        await cur.execute(
            "SELECT id FROM registry_housing_people WHERE identity_hmac=%s",
            (digest,),
        )
        registry_rows = await cur.fetchall()
        if len(registry_rows) > 1:
            summary["conflicts"] += 1
            continue
        if linked_id is not None:
            await cur.execute(
                "SELECT identity_hmac FROM registry_housing_people WHERE id=%s",
                (int(linked_id),),
            )
            linked_row = await cur.fetchone()
            if not linked_row or str(linked_row[0] or "").strip().lower() != digest:
                summary["conflicts"] += 1
                continue
            if registry_rows and int(linked_id) != int(registry_rows[0][0]):
                summary["conflicts"] += 1
                continue
        registry_id = int(linked_id or (registry_rows[0][0] if registry_rows else 0))
        if registry_id:
            summary["reused_registry_people"] += 1
        else:
            summary["created_registry_people"] += 1
        if linked_id is None:
            summary["linked_watch_people"] += 1
        await cur.execute(
            "SELECT phone_hmac, valid_from, valid_to FROM watch_person_phones "
            "WHERE person_id=%s AND phone_hmac IS NOT NULL AND phone_hmac<>'' AND phone<>'' "
            "AND (valid_from IS NULL OR valid_from<=UTC_TIMESTAMP()) "
            "AND (valid_to IS NULL OR valid_to>=UTC_TIMESTAMP())",
            (person_id,),
        )
        for phone_hmac, valid_from, valid_to in await cur.fetchall():
            if not registry_id:
                summary["new_registry_phones"] += 1
                continue
            await cur.execute(
                "SELECT id FROM registry_person_phones WHERE person_id=%s AND phone_hmac=%s "
                "AND ((valid_from=%s) OR (valid_from IS NULL AND %s IS NULL)) "
                "AND ((valid_to=%s) OR (valid_to IS NULL AND %s IS NULL)) LIMIT 1",
                (registry_id, phone_hmac, valid_from, valid_from, valid_to, valid_to),
            )
            if not await cur.fetchone():
                summary["new_registry_phones"] += 1
    return summary


async def verify_watch_people_backfill(cur) -> dict[str, int | bool]:
    checks: dict[str, int | bool] = {}
    queries = {
        "active_watch_people": "SELECT COUNT(*) FROM watch_people WHERE status='active'",
        "active_watch_people_with_identity": "SELECT COUNT(*) FROM watch_people WHERE status='active' AND identity_hmac IS NOT NULL AND identity_hmac<>''",
        "active_watch_people_linked": "SELECT COUNT(*) FROM watch_people WHERE status='active' AND registry_person_id IS NOT NULL",
        "active_registry_people": "SELECT COUNT(*) FROM registry_housing_people WHERE status='active'",
        "registry_person_phones": "SELECT COUNT(*) FROM registry_person_phones",
    }
    for key, sql in queries.items():
        await cur.execute(sql)
        checks[key] = int((await cur.fetchone())[0])
    await cur.execute(
        "SELECT COUNT(*) FROM watch_people watch JOIN registry_housing_people person "
        "ON person.id=watch.registry_person_id WHERE watch.status='active' "
        "AND watch.identity_hmac IS NOT NULL AND watch.identity_hmac=person.identity_hmac"
    )
    checks["active_link_identity_matches"] = int((await cur.fetchone())[0])
    checks["consistent"] = (
        checks["active_link_identity_matches"] == checks["active_watch_people_linked"]
    )
    return checks
