"""Dev-only relay from the Backend transactional outbox to the Dev Redis stream."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta
from typing import Any

OUTBOX = "_domain_event_outbox"
LEASE_SECONDS = 120
BATCH_SIZE = 100
TRANSPORT_RETENTION_DAYS = 30
TRANSPORT_CLEANUP_BATCH_SIZE = 200
TRANSPORT_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60


def _decode(row: tuple[Any, ...]) -> dict[str, Any]:
    legacy_names = ("event_id", "schema_version", "domain", "event_type",
             "aggregate_type", "aggregate_id", "aggregate_revision",
             "audiences_json", "status", "attempt_count", "available_at",
             "locked_by", "locked_until", "last_error_code",
             "last_error_summary", "occurred_at", "published_at")
    names = legacy_names if len(row) <= len(legacy_names) else (
        "event_id", "schema_version", "domain", "event_type",
        "aggregate_type", "aggregate_id", "aggregate_revision", "command_id",
        "task_id", "source_id", "operation_id", "changed_fields_json",
        "payload_envelope_json",
        "audiences_json", "status", "attempt_count", "available_at",
        "locked_by", "locked_until", "last_error_code",
        "last_error_summary", "occurred_at", "published_at")
    get = lambda key: row[names.index(key)] if key in names else None
    audiences = get("audiences_json")
    if isinstance(audiences, str):
        audiences = json.loads(audiences or "[]")
    if not isinstance(audiences, list) or not audiences:
        raise ValueError("event audiences missing")
    changed_fields = get("changed_fields_json")
    if isinstance(changed_fields, (str, bytes)):
        changed_fields = json.loads(changed_fields or "[]")
    encrypted_payload = get("payload_envelope_json")
    if isinstance(encrypted_payload, (str, bytes)):
        encrypted_payload = json.loads(encrypted_payload or "null")
    schema_version = int(get("schema_version") or 1)
    event = {
        "event_id": str(get("event_id")),
        "schema_version": schema_version,
        "domain": str(get("domain") or ""),
        "event_type": str(get("event_type") or ""),
        "aggregate_type": str(get("aggregate_type") or ""),
        "aggregate_id": str(get("aggregate_id") or ""),
        "aggregate_revision": int(get("aggregate_revision") or 0),
        "command_id": str(get("command_id") or ""),
        "task_id": str(get("task_id") or ""),
        "source_id": int(get("source_id")) if get("source_id") is not None else None,
        "operation_id": str(get("operation_id") or ""),
        "changed_fields": changed_fields if isinstance(changed_fields, list) else [],
        "encrypted_payload": encrypted_payload if isinstance(encrypted_payload, dict) else None,
        "audiences": [str(item) for item in audiences],
        "occurred_at": get("occurred_at").isoformat() if get("occurred_at") else None,
    }
    if schema_version == 5:
        if (event["event_type"] != "online.task.view_state_changed"
                or event["aggregate_type"] != "task_view_state"
                or not isinstance(changed_fields, dict)):
            raise ValueError("task view state event contract invalid")
        event["view_state"] = changed_fields
    return event


class BackendOutboxRelay:
    def __init__(self, pool, redis_client, config):
        self.pool = pool
        self.redis = redis_client
        self.stream = config["BACKEND_REDIS_STREAM_KEY"]
        self.worker_id = f"dev-backend-relay-{socket.gethostname()}"
        self.cleanup_enabled = bool(config.get("EVENT_TRANSPORT_CLEANUP_ENABLED", False))
        self._last_transport_cleanup = 0.0

    async def _claim(self):
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT event_id,schema_version,domain,event_type,aggregate_type,aggregate_id,"
                        f"aggregate_revision,command_id,task_id,source_id,operation_id,changed_fields_json,payload_envelope_json,"
                        f"audiences_json,status,attempt_count,available_at,locked_by,"
                        f"locked_until,last_error_code,last_error_summary,occurred_at,published_at FROM `{OUTBOX}` "
                        "WHERE status IN ('pending','retry') AND available_at<=UTC_TIMESTAMP() "
                        "ORDER BY occurred_at,event_id LIMIT %s FOR UPDATE SKIP LOCKED", (BATCH_SIZE,))
                    rows = await cur.fetchall()
                    if not rows:
                        await cur.execute(
                            f"SELECT event_id,schema_version,domain,event_type,aggregate_type,aggregate_id,"
                            f"aggregate_revision,command_id,task_id,source_id,operation_id,changed_fields_json,payload_envelope_json,"
                            f"audiences_json,status,attempt_count,available_at,locked_by,"
                            f"locked_until,last_error_code,last_error_summary,occurred_at,published_at FROM `{OUTBOX}` "
                            "WHERE status='publishing' AND locked_until<UTC_TIMESTAMP() "
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

    async def _maybe_cleanup_transport_rows(self):
        import time
        now = time.monotonic()
        if (not self.cleanup_enabled
                or now - self._last_transport_cleanup < TRANSPORT_CLEANUP_INTERVAL_SECONDS):
            return
        self._last_transport_cleanup = now
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""DELETE FROM `{OUTBOX}`
                            WHERE status='published'
                              AND published_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)
                            ORDER BY published_at,event_id
                            LIMIT %s""",
                        (TRANSPORT_RETENTION_DAYS, TRANSPORT_CLEANUP_BATCH_SIZE))
                    deleted = int(cur.rowcount or 0)
                await conn.commit()
            if deleted:
                print(json.dumps({"component": "backend-outbox-relay",
                                  "event": "transport_retention_cleanup",
                                  "deleted": deleted}), flush=True)
        except Exception:
            print(json.dumps({"component": "backend-outbox-relay",
                              "event": "transport_retention_cleanup",
                              "result": "failed",
                              "reason_code": "cleanup_query_failed"}), flush=True)

    async def run_once(self):
        rows = await self._claim()
        if not rows:
            await self._maybe_cleanup_transport_rows()
            return "idle"
        for row in rows:
            event_id = str(row[0])
            attempts = int(row[15] or 0) + 1
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
        await self._maybe_cleanup_transport_rows()
        return "published"
