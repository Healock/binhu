"""Relay durable online-summary facts from the daily ledger to Redis Streams."""
from __future__ import annotations

import json
import socket
import uuid
from typing import Any

from .online_summary_contract import validate_summary_fact


LEASE_SECONDS = 120
MAX_PAYLOAD_BYTES = 64 * 1024


class OnlineSummaryFactRelay:
    def __init__(self, pool, redis_client, *, stream: str, environment: str,
                 run_id: str, worker_id: str | None = None):
        if environment not in {"development", "staging"}:
            raise ValueError("non-Production summary relay required")
        if not stream or any(char in stream for char in "\r\n"):
            raise ValueError("invalid summary stream")
        self.pool = pool
        self.redis = redis_client
        self.stream = stream
        self.environment = environment
        self.run_id = run_id
        self.worker_id = (worker_id or f"summary-relay-{socket.gethostname()}-{uuid.uuid4().hex[:12]}")[:96]

    async def _claim(self) -> tuple[str, Any, str, int] | None:
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT event_id,fact_json,status,attempt_count FROM _online_summary_fact_outbox "
                        "WHERE status IN ('pending','retry') AND available_at<=UTC_TIMESTAMP(6) "
                        "ORDER BY created_at,event_id LIMIT 1 FOR UPDATE SKIP LOCKED"
                    )
                    row = await cur.fetchone()
                    if not row:
                        await cur.execute(
                            "SELECT event_id,fact_json,status,attempt_count FROM _online_summary_fact_outbox "
                            "WHERE status='publishing' AND locked_until<UTC_TIMESTAMP(6) "
                            "ORDER BY locked_until,created_at,event_id LIMIT 1 FOR UPDATE SKIP LOCKED"
                        )
                        row = await cur.fetchone()
                    if not row:
                        await conn.commit()
                        return None
                    event_id, raw, _status, attempt_count = row
                    await cur.execute(
                        "UPDATE _online_summary_fact_outbox SET status='publishing',locked_by=%s,"
                        "locked_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s SECOND),"
                        "attempt_count=attempt_count+1 WHERE event_id=%s",
                        (self.worker_id, LEASE_SECONDS, event_id),
                    )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return str(event_id), raw, self.worker_id, int(attempt_count or 0) + 1

    async def _finish(self, event_id: str, status: str, *, error_code: str = "",
                      delay_seconds: int = 0) -> None:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE _online_summary_fact_outbox SET status=%s,error_code=%s,"
                    "locked_by=NULL,locked_until=NULL,"
                    "available_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s SECOND),"
                    "published_at=CASE WHEN %s='published' THEN UTC_TIMESTAMP(6) ELSE published_at END "
                    "WHERE event_id=%s AND locked_by=%s AND status='publishing'",
                    (status, error_code, delay_seconds, status, event_id, self.worker_id),
                )
            await conn.commit()

    async def run_once(self) -> str:
        claimed = await self._claim()
        if claimed is None:
            return "idle"
        event_id, raw, _worker_id, attempt = claimed
        try:
            if isinstance(raw, (bytes, str)):
                raw = json.loads(raw)
            fact = validate_summary_fact(raw)
            if fact["event_id"] != event_id:
                raise ValueError("summary_fact_event_identity_mismatch")
            if fact["environment"] != self.environment or fact["run_id"] != self.run_id:
                raise ValueError("summary_fact_runtime_identity_mismatch")
            payload = json.dumps({"event_type": "online.summary.fact", "summary_fact": fact},
                                 ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                raise ValueError("summary_fact_size_exceeded")
        except Exception:
            await self._finish(event_id, "quarantined", error_code="invalid_summary_fact")
            return "quarantined"
        try:
            await self.redis.xadd(self.stream, {"event": payload, "event_id": event_id},
                                  maxlen=1_000_000, approximate=True)
        except Exception:
            await self._finish(event_id, "retry", error_code="redis_publish_failed",
                               delay_seconds=min(300, 2 ** min(attempt, 8)))
            return "retry"
        await self._finish(event_id, "published")
        return "published"


__all__ = ["OnlineSummaryFactRelay", "LEASE_SECONDS", "MAX_PAYLOAD_BYTES"]
