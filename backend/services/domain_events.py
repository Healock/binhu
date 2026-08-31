"""Transactional domain events backed by a MySQL outbox.

The outbox is deliberately small and contains identifiers/metadata only.  A
Relay publishes rows to Redis after the business transaction commits; a Redis
failure therefore never rolls back a successful business write.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Iterable


OUTBOX_TABLE = "_domain_event_outbox"
OUTBOX_STATUSES = {"pending", "publishing", "published", "retry", "blocked", "dead_letter"}


def ensure_outbox_schema_sql(table: str = OUTBOX_TABLE) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS `{table}` (
        event_id CHAR(36) PRIMARY KEY,
        schema_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
        domain VARCHAR(40) NOT NULL,
        event_type VARCHAR(100) NOT NULL,
        aggregate_type VARCHAR(80) NOT NULL,
        aggregate_id VARCHAR(160) NOT NULL,
        aggregate_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
        audiences_json JSON NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
        available_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        locked_by VARCHAR(100) DEFAULT NULL,
        locked_until DATETIME DEFAULT NULL,
        last_error_code VARCHAR(100) NOT NULL DEFAULT '',
        last_error_summary VARCHAR(500) NOT NULL DEFAULT '',
        occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        published_at DATETIME DEFAULT NULL,
        INDEX idx_domain_event_pending (status, available_at, occurred_at),
        INDEX idx_domain_event_lease (locked_until, status),
        INDEX idx_domain_event_aggregate (aggregate_type, aggregate_id, aggregate_revision)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """


async def ensure_outbox_schema(cur) -> None:
    await cur.execute(ensure_outbox_schema_sql())


def _safe_audiences(audiences: Iterable[str]) -> list[str]:
    result: list[str] = []
    for audience in audiences:
        raw = str(audience or "")
        if any(ch in raw for ch in "\r\n"):
            raise ValueError("invalid event audience")
        value = raw.strip()
        if not value or len(value) > 120:
            raise ValueError("invalid event audience")
        if not (
            value == "authenticated"
            or value == "super_admin"
            or value.startswith(("user:", "community:", "permission:"))
        ):
            raise ValueError("unsupported event audience")
        result.append(value)
    if not result:
        raise ValueError("event audience is required")
    return sorted(set(result))


def _safe_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ch in text for ch in "\r\n"):
        raise ValueError("invalid event field")
    return text


async def enqueue_event(
    cur,
    *,
    domain: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | int,
    aggregate_revision: int,
    audiences: Iterable[str],
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> str:
    """Insert an outbox event in the caller's active transaction."""
    event_id = event_id or str(uuid.uuid4())
    _safe_text(event_id, 36)
    domain = _safe_text(domain, 40)
    event_type = _safe_text(event_type, 100)
    aggregate_type = _safe_text(aggregate_type, 80)
    aggregate_id = _safe_text(aggregate_id, 160)
    if aggregate_revision < 0:
        raise ValueError("aggregate revision must be non-negative")
    audiences_json = json.dumps(_safe_audiences(audiences), ensure_ascii=False, separators=(",", ":"))
    await cur.execute(
        f"""
        INSERT INTO `{OUTBOX_TABLE}` (
            event_id, schema_version, domain, event_type, aggregate_type,
            aggregate_id, aggregate_revision, audiences_json, occurred_at
        ) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, COALESCE(%s, UTC_TIMESTAMP()))
        """,
        (event_id, domain, event_type, aggregate_type, aggregate_id,
         int(aggregate_revision), audiences_json, occurred_at),
    )
    return event_id


def decode_event_row(row: Any) -> dict[str, Any]:
    """Convert a DB tuple/dict to a safe Redis event envelope."""
    if isinstance(row, dict):
        get = row.get
    else:
        names = (
            "event_id", "schema_version", "domain", "event_type",
            "aggregate_type", "aggregate_id", "aggregate_revision",
            "audiences_json", "status", "attempt_count", "available_at",
            "locked_by", "locked_until", "last_error_code",
            "last_error_summary", "occurred_at", "published_at",
        )
        get = lambda key: row[names.index(key)]
    audiences = get("audiences_json")
    if isinstance(audiences, str):
        audiences = json.loads(audiences or "[]")
    if not isinstance(audiences, list) or not audiences:
        raise ValueError("event audiences missing")
    return {
        "event_id": str(get("event_id")),
        "schema_version": int(get("schema_version") or 1),
        "domain": str(get("domain") or ""),
        "event_type": str(get("event_type") or ""),
        "aggregate_type": str(get("aggregate_type") or ""),
        "aggregate_id": str(get("aggregate_id") or ""),
        "aggregate_revision": int(get("aggregate_revision") or 0),
        "audiences": [str(item) for item in audiences],
        "occurred_at": (get("occurred_at").isoformat() if get("occurred_at") else None),
    }
