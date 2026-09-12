"""Dev-only bridge from the Dev Backend event stream to the Kafka pipeline.

Only bounded task metadata crosses this boundary. Task values and sensitive
business text are never copied into Kafka or the derived pipeline database.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from .services.kafka_delivery_store import enqueue_delivery

PARSER_TABLES = {
    "全链条": "t_fullchain", "出租房屋核查": "t_rental_check", "涉警统计": "t_police_stats",
    "疑似未注销模型三": "t_suspect_unrevoked", "疑似返苏": "t_suspect_return",
    "寄递业": "t_delivery_industry", "群租房核查": "t_group_rental",
    "苏州涉警": "t_suzhou_police", "交通涉警": "t_traffic_police",
}
EVENT_TYPES = {"online.task.changed": "task.saved", "online.task.created": "task.created",
               "online.task.deleted": "task.deleted"}
CHANGED_FIELDS = {key: ["task_state"] for key in EVENT_TYPES}


def _source_id(parser_type: str, row_key: str) -> int:
    digest = hashlib.sha256(f"{parser_type}\x00{row_key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1) or 1


def _timestamp(value: Any) -> str:
    text = str(value or "")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text) if text else datetime.now(timezone.utc)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_to_task_event(event: Mapping[str, Any], run_id: str) -> dict[str, Any] | None:
    event_type = str(event.get("event_type") or "")
    aggregate = str(event.get("aggregate_id") or "")
    parser_type, separator, row_key = aggregate.partition(":")
    table = PARSER_TABLES.get(parser_type)
    event_id = str(event.get("event_id") or "")
    revision = event.get("aggregate_revision")
    if (event_type not in EVENT_TYPES or not separator or not row_key or not table
            or type(revision) is not int or revision < 0):
        return None
    try:
        uuid.UUID(event_id)
    except (ValueError, AttributeError):
        return None
    source_id = _source_id(parser_type, row_key)
    return {"schema_version": 1, "event_id": event_id,
            "event_type": EVENT_TYPES[event_type],
            "task_id": f"{table}:{source_id}", "source_id": source_id,
            "revision": revision, "operation_id": event_id,
            "changed_fields": CHANGED_FIELDS[event_type],
            "timestamp": _timestamp(event.get("occurred_at")),
            "environment": "development", "run_id": run_id}


async def run(config: Mapping[str, str], pool) -> None:
    """Continuously copy Dev metadata events into the independent ledger."""
    from redis.asyncio import Redis

    url = config.get("BACKEND_REDIS_URL", "")
    if not url or "production" in url.lower() or "staging" in url.lower():
        raise ValueError("development backend Redis is required")
    # XREAD is deliberately held open for up to five seconds.  A read timeout
    # equal to the block interval turns an idle stream into a worker crash,
    # which can lose the next event while the container is restarting.  Keep
    # connection establishment bounded, but let the blocking read complete.
    client = Redis.from_url(url, decode_responses=True, socket_timeout=None,
                            socket_connect_timeout=5)
    stream = config.get("BACKEND_REDIS_STREAM_KEY", "binhu:events")
    cursor = config.get("BACKEND_REDIS_START_ID", "$")
    try:
        await client.ping()
        while True:
            batches = await client.xread({stream: cursor}, count=100, block=5000)
            for _stream_name, entries in batches or []:
                for stream_id, values in entries:
                    cursor = stream_id
                    raw = values.get("event") if isinstance(values, dict) else None
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    converted = event_to_task_event(event, config["DEV_RUN_ID"])
                    if converted is None:
                        continue
                    async with pool.acquire() as conn:
                        try:
                            await conn.begin()
                            async with conn.cursor() as cur:
                                await enqueue_delivery(cur, converted, run_id=config["DEV_RUN_ID"])
                            await conn.commit()
                        except BaseException:
                            await conn.rollback()
                            raise
    finally:
        await client.aclose()
