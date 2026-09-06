"""MySQL delivery ledger isolated from the Redis/SSE Outbox status.

Only an explicitly prepared shadow database may instantiate this store. Schema
creation is an operator step, never part of the Backend request lifecycle.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager

from services.kafka_envelope import (
    serialize_event, event_partition_key, validate_event,
)
from services.kafka_relay import Delivery, LEASE_SECONDS, MAX_ATTEMPTS


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS _kafka_event_delivery (
 event_id CHAR(36) PRIMARY KEY,
 run_id VARCHAR(80) NOT NULL,
 event_json JSON NOT NULL,
 payload_sha256 CHAR(64) NOT NULL,
 status VARCHAR(24) NOT NULL DEFAULT 'pending',
 event_attempts INT UNSIGNED NOT NULL DEFAULT 0,
 dlq_attempts INT UNSIGNED NOT NULL DEFAULT 0,
 lease_token CHAR(36) NULL,
 locked_until DATETIME(6) NULL,
 available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 published_at DATETIME(6) NULL,
 last_error_code VARCHAR(48) NOT NULL DEFAULT '',
 INDEX pending_delivery (run_id, status, available_at, created_at),
 INDEX expired_delivery (run_id, status, locked_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
"""


async def enqueue_delivery(cur, event: dict, *, run_id: str) -> None:
    """Append Kafka intent using the SAME cursor/transaction as business Outbox.

    The caller owns commit/rollback. Reusing an event id with changed metadata
    fails the transaction instead of silently mutating an earlier event.
    """
    normalized = validate_event(event)
    if normalized["run_id"] != run_id:
        raise ValueError("Kafka delivery run mismatch")
    payload = serialize_event(normalized)
    digest = hashlib.sha256(payload).hexdigest()
    await cur.execute(
        """INSERT INTO _kafka_event_delivery
             (event_id, run_id, event_json, payload_sha256)
           VALUES (%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE event_id=event_id""",
        (normalized["event_id"], run_id, payload.decode("utf-8"), digest),
    )
    await cur.execute(
        "SELECT run_id,payload_sha256 FROM _kafka_event_delivery WHERE event_id=%s",
        (normalized["event_id"],),
    )
    if await cur.fetchone() != (run_id, digest):
        raise ValueError("Kafka event id conflicts with existing metadata")


class MySQLDeliveryStore:
    def __init__(self, pool, *, run_id: str):
        if not run_id.startswith("KSHADOW-"):
            raise ValueError("Kafka delivery requires a shadow run")
        self.pool, self.run_id = pool, run_id

    @asynccontextmanager
    async def _connection(self):
        conn = await asyncio.wait_for(self.pool.acquire(), timeout=5)
        try:
            yield conn
        finally:
            self.pool.release(conn)

    async def claim(self) -> Delivery | None:
        async with self._connection() as conn:
            try:
                await conn.begin()
                async with conn.cursor() as cur:
                    await cur.execute(
                        """SELECT event_id,event_json,payload_sha256,status,
                                  event_attempts,dlq_attempts
                           FROM _kafka_event_delivery
                           WHERE run_id=%s AND (
                             (status IN ('pending','retry','dlq_pending')
                              AND available_at<=UTC_TIMESTAMP(6)) OR
                             (status IN ('publishing','dlq_publishing')
                              AND locked_until<UTC_TIMESTAMP(6)))
                           ORDER BY created_at,event_id LIMIT 1
                           FOR UPDATE SKIP LOCKED""",
                        (self.run_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        await conn.commit()
                        return None
                    event_id, raw, digest, status, attempts, dlq_attempts = row
                    try:
                        event = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                        payload = serialize_event(event)
                        if event["run_id"] != self.run_id or event["event_id"] != event_id:
                            raise ValueError("stored metadata identity mismatch")
                        if hashlib.sha256(payload).hexdigest() != digest:
                            raise ValueError("stored metadata hash mismatch")
                        key = event_partition_key(event)
                    except (ValueError, TypeError, KeyError):
                        await cur.execute(
                            """UPDATE _kafka_event_delivery SET status='quarantined',
                               last_error_code='invalid_stored_metadata',lease_token=NULL,
                               locked_until=NULL WHERE event_id=%s AND run_id=%s""",
                            (event_id, self.run_id),
                        )
                        await conn.commit()
                        return None
                    channel = "dlq" if status.startswith("dlq") or attempts >= MAX_ATTEMPTS else "events"
                    count = dlq_attempts if channel == "dlq" else attempts
                    if count >= MAX_ATTEMPTS:
                        await cur.execute(
                            """UPDATE _kafka_event_delivery SET status='blocked',
                               last_error_code='dlq_attempts_exhausted',lease_token=NULL,
                               locked_until=NULL WHERE event_id=%s AND run_id=%s""",
                            (event_id, self.run_id),
                        )
                        await conn.commit()
                        return None
                    token = str(uuid.uuid4())
                    await cur.execute(
                        """UPDATE _kafka_event_delivery SET status=%s,lease_token=%s,
                           locked_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s SECOND),
                           event_attempts=event_attempts+%s,dlq_attempts=dlq_attempts+%s
                           WHERE event_id=%s AND run_id=%s""",
                        ("publishing" if channel == "events" else "dlq_publishing",
                         token, LEASE_SECONDS, int(channel == "events"), int(channel == "dlq"),
                         event_id, self.run_id),
                    )
                await conn.commit()
                return Delivery(event_id, payload, key, token, count + 1, channel,
                                event_type=event["event_type"])
            except BaseException:
                await conn.rollback()
                raise

    async def finish(self, delivery: Delivery, *, status: str, error_code: str,
                     delay_seconds: float) -> bool:
        allowed = {"published", "retry", "dlq_pending", "dead_letter", "blocked"}
        if status not in allowed or not 0 <= delay_seconds <= 72:
            raise ValueError("invalid delivery completion")
        async with self._connection() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """UPDATE _kafka_event_delivery SET status=%s,last_error_code=%s,
                           lease_token=NULL,locked_until=NULL,
                           available_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s MICROSECOND),
                           published_at=CASE WHEN %s IN ('published','dead_letter')
                             THEN UTC_TIMESTAMP(6) ELSE published_at END
                           WHERE event_id=%s AND run_id=%s AND lease_token=%s
                             AND status=%s AND locked_until>UTC_TIMESTAMP(6)""",
                        (status,error_code,int(delay_seconds*1_000_000),status,
                         delivery.event_id,self.run_id,delivery.lease_token,
                         "publishing" if delivery.channel == "events" else "dlq_publishing"),
                    )
                    owned = cur.rowcount == 1
                await conn.commit()
                return owned
            except BaseException:
                await conn.rollback()
                raise
