"""Dev-only relay from the Backend transactional outbox to the Dev Redis stream."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta
from typing import Any

OUTBOX = "_domain_event_outbox"
LEASE_SECONDS = 120
BATCH_SIZE = 100


def _decode(row: tuple[Any, ...]) -> dict[str, Any]:
    names = ("event_id", "schema_version", "domain", "event_type",
             "aggregate_type", "aggregate_id", "aggregate_revision",
             "audiences_json", "status", "attempt_count", "available_at",
             "locked_by", "locked_until", "last_error_code",
             "last_error_summary", "occurred_at", "published_at")
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
        "occurred_at": get("occurred_at").isoformat() if get("occurred_at") else None,
    }


class BackendOutboxRelay:
    def __init__(self, pool, redis_client, config):
        self.pool = pool
        self.redis = redis_client
        self.stream = config["BACKEND_REDIS_STREAM_KEY"]
        self.worker_id = f"dev-backend-relay-{socket.gethostname()}"

    async def _claim(self):
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT event_id,schema_version,domain,event_type,aggregate_type,aggregate_id,"
                        f"aggregate_revision,audiences_json,status,attempt_count,available_at,locked_by,"
                        f"locked_until,last_error_code,last_error_summary,occurred_at,published_at FROM `{OUTBOX}` "
                        "WHERE (status IN ('pending','retry') AND available_at<=UTC_TIMESTAMP()) "
                        "OR (status='publishing' AND locked_until<UTC_TIMESTAMP()) "
                        "ORDER BY occurred_at,event_id LIMIT %s FOR UPDATE SKIP LOCKED", (BATCH_SIZE,))
                    rows = await cur.fetchall()
                    if rows:
                        ids = [row[0] for row in rows]
                        marks = ",".join(["%s"] * len(ids))
                        await cur.execute(
                            f"UPDATE `{OUTBOX}` SET status='publishing',locked_by=%s,"
                            f"locked_until=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND) WHERE event_id IN ({marks})",
                            (self.worker_id, LEASE_SECONDS, *ids))
                await conn.commit()
                return rows
            except BaseException:
                await conn.rollback()
                raise

    async def _finish(self, event_id, status, attempts, error_code="", available_at=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"UPDATE `{OUTBOX}` SET status=%s,attempt_count=%s,locked_by=NULL,locked_until=NULL,"
                    "last_error_code=%s,last_error_summary='',available_at=COALESCE(%s,available_at),"
                    "published_at=CASE WHEN %s='published' THEN UTC_TIMESTAMP() ELSE published_at END "
                    "WHERE event_id=%s AND locked_by=%s",
                    (status, attempts, error_code[:100], available_at, status, event_id, self.worker_id))
            await conn.commit()

    async def run_once(self):
        rows = await self._claim()
        if not rows:
            return "idle"
        for row in rows:
            event_id = str(row[0])
            attempts = int(row[9] or 0) + 1
            try:
                event = _decode(row)
                payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                if len(payload.encode("utf-8")) > 256 * 1024:
                    raise ValueError("event payload exceeds size limit")
                await self.redis.xadd(self.stream, {"event": payload, "event_id": event_id},
                                       maxlen=1_000_000, approximate=True)
                await self._finish(event_id, "published", attempts)
            except Exception as exc:
                delay = min(300, 2 ** min(attempts, 8))
                await self._finish(event_id, "retry", attempts, type(exc).__name__,
                                   datetime.utcnow() + timedelta(seconds=delay))
        return "published"
