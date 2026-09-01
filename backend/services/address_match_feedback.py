"""Privacy-preserving memory for manually confirmed address matches.

The online matcher may reuse a confirmation only for the exact same
normalized address within the same formal community.  Plain addresses are
never copied into the feedback tables.  Conflicting confirmations disable the
memory instead of allowing one mistaken click to affect later tasks.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Iterable

from services.address_matching import MATCHER_VERSION, normalize_address_text


FEEDBACK_VERSION = "address-feedback-v1"
ACTIVE = "active"
CONFLICT = "conflict"


def feedback_hmac(address: Any, community_name: Any) -> str:
    from config import settings

    normalized_address = normalize_address_text(address)
    normalized_community = normalize_address_text(community_name)
    if not normalized_address or not normalized_community:
        return ""
    payload = (
        f"{FEEDBACK_VERSION}:{normalized_community}:{normalized_address}"
    ).encode("utf-8")
    return hmac.new(
        settings.registry_hmac_key.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class FeedbackTransition:
    status: str
    confirmed_entry_id: int | None
    confirmation_count: int
    conflict_count: int


def feedback_transition(
    existing: dict[str, Any] | None,
    *,
    confirmed_entry_id: int,
    community_id: int,
) -> FeedbackTransition:
    """Calculate a safe aggregate transition without touching the database."""
    if not existing:
        return FeedbackTransition(ACTIVE, confirmed_entry_id, 1, 0)

    confirmations = int(existing.get("confirmation_count") or 0) + 1
    conflicts = int(existing.get("conflict_count") or 0)
    existing_status = str(existing.get("status") or ACTIVE)
    existing_entry = existing.get("confirmed_entry_id")
    existing_community = existing.get("community_id")
    same_target = (
        existing_status == ACTIVE
        and existing_entry is not None
        and int(existing_entry) == int(confirmed_entry_id)
        and existing_community is not None
        and int(existing_community) == int(community_id)
    )
    if same_target:
        return FeedbackTransition(ACTIVE, confirmed_entry_id, confirmations, conflicts)
    return FeedbackTransition(CONFLICT, None, confirmations, conflicts + 1)


async def ensure_address_match_feedback_schema(cur) -> None:
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _online_address_match_feedback (
            address_hmac CHAR(64) NOT NULL PRIMARY KEY,
            community_id BIGINT NOT NULL,
            confirmed_entry_id BIGINT DEFAULT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            confirmation_count INT NOT NULL DEFAULT 0,
            conflict_count INT NOT NULL DEFAULT 0,
            matcher_version VARCHAR(40) NOT NULL DEFAULT '',
            first_confirmed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_confirmed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_confirmed_by BIGINT DEFAULT NULL,
            INDEX idx_address_feedback_target (
                status, community_id, confirmed_entry_id
            )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _online_address_match_feedback_events (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            address_hmac CHAR(64) NOT NULL,
            community_id BIGINT NOT NULL,
            confirmed_entry_id BIGINT NOT NULL,
            confirmed_by BIGINT NOT NULL,
            matcher_version VARCHAR(40) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_address_feedback_event_key (address_hmac, created_at),
            INDEX idx_address_feedback_event_task (parser_type, row_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )


async def load_feedback_memories(
    cur,
    address_contexts: Iterable[tuple[Any, Any]],
) -> dict[str, dict[str, Any]]:
    keys = sorted({
        key
        for address, community_name in address_contexts
        if (key := feedback_hmac(address, community_name))
    })
    if not keys:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(keys), 500):
        chunk = keys[offset:offset + 500]
        placeholders = ",".join(["%s"] * len(chunk))
        await cur.execute(
            "SELECT address_hmac,community_id,confirmed_entry_id,status,"
            "confirmation_count,conflict_count,matcher_version "
            f"FROM _online_address_match_feedback WHERE address_hmac IN ({placeholders})",
            chunk,
        )
        for row in await cur.fetchall():
            result[str(row[0])] = {
                "community_id": int(row[1]),
                "confirmed_entry_id": int(row[2]) if row[2] is not None else None,
                "status": str(row[3] or ""),
                "confirmation_count": int(row[4] or 0),
                "conflict_count": int(row[5] or 0),
                "matcher_version": str(row[6] or ""),
            }
    return result


def apply_feedback_memory(
    match_result: dict[str, Any],
    *,
    address: Any,
    community_name: Any,
    memories: dict[str, dict[str, Any]],
    entries_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Reuse an unambiguous exact-address confirmation as an automatic match."""
    if str(match_result.get("status") or "") in {"conflict", "invalid", "confirmed"}:
        return match_result
    key = feedback_hmac(address, community_name)
    memory = memories.get(key)
    if not key or not memory or memory.get("status") != ACTIVE:
        return match_result
    entry_id = memory.get("confirmed_entry_id")
    entry = entries_by_id.get(int(entry_id or 0))
    if not entry or not entry.get("enabled", True):
        return match_result
    if int(entry.get("community_id") or 0) != int(memory.get("community_id") or 0):
        return match_result
    if normalize_address_text(entry.get("community_name")) != normalize_address_text(
        community_name
    ):
        return match_result
    candidate = {
        "entry_id": int(entry["id"]),
        "name": str(entry.get("name") or ""),
        "community_id": int(entry["community_id"]),
        "community_name": str(entry.get("community_name") or ""),
        "score": 1.0,
        "method": "人工反馈记忆",
        "reason": "同一规范化地址已由人工确认",
    }
    candidates = list(match_result.get("candidates") or [])
    if not any(int(item.get("entry_id") or 0) == candidate["entry_id"] for item in candidates):
        candidates.insert(0, candidate)
    return {
        "status": "suggested",
        "score": 1.0,
        "method": "人工反馈记忆",
        "reason": "同一规范化地址已由人工确认，自动复用",
        "candidate": candidate,
        "candidates": candidates,
        "version": MATCHER_VERSION,
    }


async def record_feedback_confirmation(
    cur,
    *,
    parser_type: str,
    row_key: str,
    address: Any,
    community_name: Any,
    community_id: int,
    confirmed_entry_id: int,
    confirmed_by: int,
) -> str:
    key = feedback_hmac(address, community_name)
    if not key:
        return "ignored"
    await cur.execute(
        "SELECT community_id,confirmed_entry_id,status,confirmation_count,"
        "conflict_count FROM _online_address_match_feedback "
        "WHERE address_hmac=%s FOR UPDATE",
        (key,),
    )
    row = await cur.fetchone()
    existing = None
    if row:
        existing = {
            "community_id": row[0],
            "confirmed_entry_id": row[1],
            "status": row[2],
            "confirmation_count": row[3],
            "conflict_count": row[4],
        }
    transition = feedback_transition(
        existing,
        confirmed_entry_id=confirmed_entry_id,
        community_id=community_id,
    )
    if row:
        await cur.execute(
            "UPDATE _online_address_match_feedback SET community_id=%s,"
            "confirmed_entry_id=%s,status=%s,confirmation_count=%s,"
            "conflict_count=%s,matcher_version=%s,last_confirmed_at=UTC_TIMESTAMP(),"
            "last_confirmed_by=%s WHERE address_hmac=%s",
            (
                community_id,
                transition.confirmed_entry_id,
                transition.status,
                transition.confirmation_count,
                transition.conflict_count,
                MATCHER_VERSION,
                confirmed_by,
                key,
            ),
        )
    else:
        await cur.execute(
            "INSERT INTO _online_address_match_feedback (address_hmac,community_id,"
            "confirmed_entry_id,status,confirmation_count,conflict_count,matcher_version,"
            "first_confirmed_at,last_confirmed_at,last_confirmed_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(),UTC_TIMESTAMP(),%s)",
            (
                key,
                community_id,
                transition.confirmed_entry_id,
                transition.status,
                transition.confirmation_count,
                transition.conflict_count,
                MATCHER_VERSION,
                confirmed_by,
            ),
        )
    await cur.execute(
        "INSERT INTO _online_address_match_feedback_events (parser_type,row_key,"
        "address_hmac,community_id,confirmed_entry_id,confirmed_by,matcher_version) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            parser_type,
            row_key,
            key,
            community_id,
            confirmed_entry_id,
            confirmed_by,
            MATCHER_VERSION,
        ),
    )
    return transition.status
