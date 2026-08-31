"""Transactional outbox relay for Redis Streams.

This process is intentionally separate from the web server.  Business
transactions only append to MySQL; the relay provides at-least-once delivery
to Redis and never makes a successful business write depend on Redis.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import socket
import time
from datetime import datetime, timedelta
from typing import Any

import redis.asyncio as redis
from services.domain_events import OUTBOX_TABLE, decode_event_row


RETRY_DELAYS = (1, 2, 5, 10, 30, 60, 120, 300)
LEASE_SECONDS = 120
BATCH_SIZE = 100


def _settings():
    from config import settings
    return settings


def retry_delay(attempt: int, *, jitter: bool = True) -> float:
    base = RETRY_DELAYS[min(max(int(attempt) - 1, 0), len(RETRY_DELAYS) - 1)]
    if int(attempt) > len(RETRY_DELAYS):
        base = 300
    return base * random.uniform(0.8, 1.2) if jitter else float(base)


def classify_redis_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if any(token in text for token in ("noauth", "wrongpass", "invalid password")):
        return "blocked"
    if any(token in text for token in ("wrongtype", "event", "payload", "required", "size")):
        return "dead_letter"
    if any(token in text for token in ("connection", "timeout", "loading", "readonly", "max number", "maxclients", "oom", "memory")):
        return "retry"
    return "retry"


def is_transient_redis_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, (redis.ConnectionError, redis.TimeoutError, OSError)) or any(
        token in text for token in ("loading", "readonly", "maxclients", "max number", "oom", "out of memory", "memory")
    )


def _stream_payload(event: dict[str, Any]) -> dict[str, str]:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 256 * 1024:
        raise ValueError("event payload exceeds size limit")
    return {"event": payload, "event_id": event["event_id"]}


class OutboxRelay:
    def __init__(self, redis_client: Any | None = None) -> None:
        self.redis = redis_client
        self.worker_id = f"relay-{socket.gethostname()}-{os.getpid()}"
        self._consecutive_failures = 0
        self._breaker_until = 0.0
        self._breaker_level = 0
        self._last_trim = 0.0

    async def connect(self) -> None:
        if self.redis is None:
            self.redis = redis.from_url(_settings().REDIS_URL, decode_responses=True)
        await self.redis.ping()

    async def _lease_batch(self, pool) -> list[tuple[Any, ...]]:
        conn = await pool.acquire()
        try:
            await conn.begin()
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT event_id, schema_version, domain, event_type,
                           aggregate_type, aggregate_id, aggregate_revision,
                           audiences_json, status, attempt_count, available_at,
                           locked_by, locked_until, last_error_code,
                           last_error_summary, occurred_at, published_at
                    FROM `{OUTBOX_TABLE}`
                    WHERE (status IN ('pending','retry') AND available_at<=UTC_TIMESTAMP())
                       OR (status='publishing' AND locked_until<UTC_TIMESTAMP())
                    ORDER BY occurred_at, event_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (BATCH_SIZE,),
                )
                rows = await cur.fetchall()
                if rows:
                    ids = [row[0] for row in rows]
                    placeholders = ",".join(["%s"] * len(ids))
                    await cur.execute(
                        f"""UPDATE `{OUTBOX_TABLE}`
                            SET status='publishing', locked_by=%s,
                                locked_until=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND)
                            WHERE event_id IN ({placeholders})""",
                        (self.worker_id, LEASE_SECONDS, *ids),
                    )
            await conn.commit()
            return rows
        except Exception:
            await conn.rollback()
            raise
        finally:
            pool.release(conn)

    async def _finish(self, pool, event_id: str, *, status: str,
                      attempt_count: int, error_code: str = "",
                      error_summary: str = "", available_at: datetime | None = None) -> None:
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""UPDATE `{OUTBOX_TABLE}`
                        SET status=%s, attempt_count=%s, locked_by=NULL,
                            locked_until=NULL, last_error_code=%s,
                            last_error_summary=%s,
                            available_at=COALESCE(%s, available_at),
                            published_at=CASE WHEN %s='published' THEN UTC_TIMESTAMP() ELSE published_at END
                        WHERE event_id=%s AND locked_by=%s""",
                    (status, attempt_count, error_code, error_summary[:500], available_at,
                     status, event_id, self.worker_id),
                )
            await conn.commit()
        finally:
            pool.release(conn)

    async def publish_batch(self, pool) -> int:
        rows = await self._lease_batch(pool)
        published = 0
        for row in rows:
            event_id = str(row[0])
            attempts = int(row[9] or 0) + 1
            try:
                event = decode_event_row(row)
                fields = _stream_payload(event)
                stream_id = await self.redis.xadd(
                    _settings().REDIS_STREAM_KEY,
                    fields,
                    maxlen=_settings().REDIS_STREAM_MAX_ENTRIES,
                    approximate=True,
                )
                try:
                    await self.redis.publish(_settings().REDIS_PUBSUB_CHANNEL, str(stream_id))
                except Exception:
                    # Stream is authoritative; Pub/Sub is only a wake-up hint.
                    pass
                await self._finish(pool, event_id, status="published", attempt_count=attempts)
                published += 1
            except Exception as exc:
                kind = classify_redis_error(exc)
                if kind == "dead_letter" or (kind == "blocked" and attempts >= 3) or (kind == "retry" and attempts >= 12 and not is_transient_redis_error(exc)):
                    status = "dead_letter" if kind != "blocked" else "blocked"
                    await self._finish(pool, event_id, status=status, attempt_count=attempts,
                                       error_code=type(exc).__name__, error_summary=str(exc))
                else:
                    wait = retry_delay(attempts)
                    available = datetime.utcnow() + timedelta(seconds=wait)
                    await self._finish(pool, event_id, status="retry", attempt_count=attempts,
                                       error_code=type(exc).__name__, error_summary=str(exc), available_at=available)
        return published

    async def run_once(self) -> int:
        if self.redis is None:
            await self.connect()
        if time.monotonic() - self._last_trim > 60:
            cfg = _settings()
            cutoff_ms = int((time.time() - cfg.REDIS_STREAM_RETENTION_DAYS * 86400) * 1000)
            await self.redis.xtrim(cfg.REDIS_STREAM_KEY, minid=f"{cutoff_ms}-0", approximate=True)
            self._last_trim = time.monotonic()
        now = time.monotonic()
        if now < self._breaker_until:
            return 0
        total = 0
        from database import db_manager
        for name in ("online_data", "platform"):
            try:
                total += await self.publish_batch(db_manager.get_pool(name))
                self._consecutive_failures = 0
            except (redis.ConnectionError, redis.TimeoutError, OSError) as exc:
                self._consecutive_failures += 1
                if self._consecutive_failures >= 5:
                    self._breaker_level = min(self._breaker_level + 1, 3)
                    self._breaker_until = time.monotonic() + (30, 60, 120, 300)[self._breaker_level]
                raise exc
        return total


async def main() -> None:
    from database import init_db, close_db
    await init_db()
    relay = OutboxRelay()
    try:
        while True:
            try:
                await relay.run_once()
                await asyncio.sleep(0.5)
            except Exception:
                await asyncio.sleep(1.0)
    finally:
        if relay.redis is not None:
            await relay.redis.aclose()
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
