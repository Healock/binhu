"""Dev-only bridge from the Backend event stream to the Kafka pipeline.

Task metadata is bounded and allowlisted. Business snapshots may cross only as
AES-GCM ciphertext; this bridge never decrypts them or writes plaintext to Kafka,
the derived pipeline database, Redis, logs, or the dead-letter topic.
"""
from __future__ import annotations

import hashlib
import json
import uuid
import socket
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from .services.kafka_delivery_store import enqueue_delivery
from .services.kafka_event_contract import validate_task_event
from .services.online_summary_event import summary_fact_from_bridge_event

PARSER_TABLES = {
    "全链条": "t_fullchain", "出租房屋核查": "t_rental_check", "涉警统计": "t_police_stats",
    "疑似未注销模型三": "t_suspect_unrevoked", "疑似返苏": "t_suspect_return",
    "寄递业": "t_delivery_industry", "群租房核查": "t_group_rental",
    "苏州涉警": "t_suzhou_police", "交通涉警": "t_traffic_police",
}
EVENT_TYPES = {"online.task.changed": "task.saved", "online.task.created": "task.created",
               "online.task.deleted": "task.deleted",
               "online.task.claimed": "task.claimed",
               "online.task.assigned": "task.assigned",
               "online.task.reviewed": "task.reviewed",
               "online.task.archived": "task.archived"}
CHANGED_FIELDS = {key: ["task_state"] for key in EVENT_TYPES}
SAFE_CHANGED_FIELDS = {
    "address", "community", "inspector", "check_result", "task_state",
    "small_community", "person_tags", "task_graph", "online_summary",
}
TABLE_PARSERS = {table: parser for parser, table in PARSER_TABLES.items()}


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


def event_to_task_event(
    event: Mapping[str, Any], run_id: str, environment: str = "development",
) -> dict[str, Any] | None:
    event_type = str(event.get("event_type") or "")
    event_id = str(event.get("event_id") or "")
    revision = event.get("aggregate_revision")
    schema_version = event.get("schema_version", 1)
    if (schema_version == 5 and event_type == "online.task.view_state_changed"):
        try:
            view_state = event.get("view_state")
            if not isinstance(view_state, dict):
                return None
            domain = str(view_state.get("domain") or "")
            task_revision = view_state.get("task_revision")
            removed = view_state.get("removed")
            task_id = str(event.get("task_id") or "")
            source_id = event.get("source_id")
            revision = event.get("aggregate_revision")
            event_id = str(event.get("event_id") or "")
            operation_id = str(event.get("operation_id") or "")
            uuid.UUID(event_id)
            uuid.UUID(operation_id)
            if (domain not in {"address_match", "watch", "qmf", "residence", "registration", "review", "task_graph"}
                    or type(task_revision) is not int or task_revision < 1
                    or type(removed) is not bool or type(revision) is not int or revision < 1
                    or type(source_id) is not int or source_id < 1
                    or not isinstance(event.get("encrypted_payload"), dict)):
                return None
            return validate_task_event({
                "schema_version": 5,
                "event_id": event_id,
                "event_type": "task.view_state.removed" if removed else "task.view_state.changed",
                "task_id": task_id,
                "source_id": source_id,
                "revision": revision,
                "operation_id": operation_id,
                "changed_fields": ["task_state"],
                "timestamp": _timestamp(event.get("occurred_at")),
                "environment": environment,
                "run_id": run_id,
                "view_domain": domain,
                "task_revision": task_revision,
                "encrypted_payload": event["encrypted_payload"],
            })
        except (TypeError, ValueError):
            return None
    if (event_type not in EVENT_TYPES or type(revision) is not int or revision < 0
            or type(schema_version) is not int or schema_version not in {3, 4, 6}):
        return None
    try:
        uuid.UUID(event_id)
    except (ValueError, AttributeError):
        return None
    if event.get("domain") != "online" or event.get("aggregate_type") != "online_task":
        return None
    task_id = str(event.get("task_id") or "")
    command_id = str(event.get("command_id") or "")
    source_id = event.get("source_id")
    operation_id = str(event.get("operation_id") or "")
    changed_fields = event.get("changed_fields")
    aggregate_id = str(event.get("aggregate_id") or "")
    table, separator, physical_row = task_id.partition(":")
    if (not separator or table not in TABLE_PARSERS or not physical_row.isdecimal()
            or len(physical_row) > 19 or (len(physical_row) == 19 and physical_row > str(2**63 - 1))
            or int(physical_row) <= 0 or len(task_id) > 96 or aggregate_id != task_id
            or type(source_id) is not int or source_id <= 0
            or not operation_id or not isinstance(changed_fields, list)
            or not changed_fields or len(changed_fields) > len(SAFE_CHANGED_FIELDS)
            or any(not isinstance(item, str) or item not in SAFE_CHANGED_FIELDS
                   for item in changed_fields)
            or len(set(changed_fields)) != len(changed_fields)
            or revision <= 0):
        return None
    try:
        uuid.UUID(operation_id)
    except ValueError:
        return None
    task_event = {"schema_version": schema_version, "event_id": event_id,
            "event_type": EVENT_TYPES[event_type],
            "task_id": task_id, "source_id": source_id,
            "revision": revision, "operation_id": operation_id,
            "changed_fields": changed_fields,
            "timestamp": _timestamp(event.get("occurred_at")),
            "environment": environment, "run_id": run_id}
    encrypted_payload = event.get("encrypted_payload")
    if event_type == "online.task.deleted":
        if encrypted_payload is not None:
            return None
    elif not isinstance(encrypted_payload, dict):
        return None
    else:
        task_event["encrypted_payload"] = encrypted_payload
    if schema_version in {4, 6}:
        try:
            uuid.UUID(command_id)
        except ValueError:
            return None
        task_event["command_id"] = command_id
    if schema_version == 6 and event_type != "online.task.deleted":
        try:
            import base64
            aad = encrypted_payload.get("aad") if isinstance(encrypted_payload, dict) else None
            aad_bytes = base64.b64decode(aad + "=" * (-len(aad) % 4), altchars=b"-_", validate=True)
            aad_object = json.loads(aad_bytes)
            row_group_token = aad_object.get("row_group_token")
            if (not isinstance(row_group_token, str)
                    or len(row_group_token) != 64
                    or any(char not in "0123456789abcdef" for char in row_group_token)):
                return None
            uuid.UUID(command_id)
        except (ValueError, TypeError, AttributeError):
            return None
        task_event["row_group_token"] = row_group_token
    try:
        return validate_task_event(task_event)
    except (TypeError, ValueError):
        return None


async def run(config: Mapping[str, str], pool) -> None:
    """Continuously copy events into the independent ledger.

    Redis Streams consumer groups provide the durable cursor.  A message is
    acknowledged only after the delivery-ledger transaction commits; a
    connection or database failure therefore leaves it pending for a later
    consumer (including after a process restart).
    """
    from redis.asyncio import Redis

    url = config.get("BACKEND_REDIS_URL", "")
    environment = config["APP_ENVIRONMENT"]
    run_id = config.get("RUN_ID") or config.get("DEV_RUN_ID") or config.get("STAGING_RUN_ID")
    forbidden = {"production", "shadow", "development", "staging"} - {environment}
    if not url or any(value in url.lower() for value in forbidden):
        raise ValueError("isolated non-Production backend Redis is required")
    # XREAD is deliberately held open for up to five seconds.  A read timeout
    # equal to the block interval turns an idle stream into a worker crash,
    # which can lose the next event while the container is restarting.  Keep
    # connection establishment bounded, but let the blocking read complete.
    client = Redis.from_url(url, decode_responses=True, socket_timeout=None,
                            socket_connect_timeout=5)
    stream = config.get("BACKEND_REDIS_STREAM_KEY", "binhu:events")
    # The group is stable for a pipeline run, while the consumer identity is
    # unique to this process so XAUTOCLAIM can recover a dead consumer.
    group = config.get("BACKEND_REDIS_CONSUMER_GROUP") or f"{run_id}-business-bridge"
    consumer = config.get("BACKEND_REDIS_CONSUMER_NAME") or (
        f"bridge-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    )
    if any(ch in group or ch in consumer for ch in "\r\n"):
        raise ValueError("invalid Redis consumer identity")
    start_id = config.get("BACKEND_REDIS_START_ID", "0-0")

    async def ensure_group() -> None:
        try:
            await client.xgroup_create(stream, group, id=start_id, mkstream=True)
        except Exception as exc:
            # BUSYGROUP means another worker already established the durable
            # group.  Any other error is a real connectivity/configuration
            # failure and must stop the bridge without losing the cursor.
            if "BUSYGROUP" not in str(exc).upper():
                raise

    async def recover_pending() -> list[tuple[str, dict[str, str]]]:
        result = await client.xautoclaim(
            stream, group, consumer, min_idle_time=60_000,
            start_id="0-0", count=100,
        )
        # redis-py returns (next-start-id, entries, deleted-entry-ids).
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            return list(result[1] or [])
        return []

    async def read_new() -> list[tuple[str, dict[str, str]]]:
        batches = await client.xreadgroup(
            group, consumer, {stream: ">"}, count=100, block=5000,
        )
        entries: list[tuple[str, dict[str, str]]] = []
        for _stream_name, batch in batches or []:
            entries.extend(batch or [])
        return entries

    async def process(entry_id: str, values: Mapping[str, Any]) -> None:
        raw = values.get("event") if isinstance(values, Mapping) else None
        if not raw:
            # An empty stream entry is terminally unusable.  Acknowledge it so
            # one poison entry cannot block all later valid business events.
            await client.xack(stream, group, entry_id)
            return
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            await client.xack(stream, group, entry_id)
            return
        converted = event_to_task_event(event, run_id, environment)
        if converted is None:
            try:
                converted = summary_fact_from_bridge_event(
                    event, environment=environment, run_id=run_id,
                )
            except (TypeError, ValueError):
                converted = None
        if converted is None:
            # Contract rejection is a terminal input validation result; no
            # business write was attempted and the raw body is never logged.
            await client.xack(stream, group, entry_id)
            return
        async with pool.acquire() as conn:
            try:
                await conn.begin()
                async with conn.cursor() as cur:
                    await enqueue_delivery(cur, converted, run_id=run_id)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        # The ACK is deliberately after commit.  If this call fails, the
        # pending entry is recovered by XAUTOCLAIM and the ledger's event_id
        # uniqueness makes replay idempotent.
        await client.xack(stream, group, entry_id)

    try:
        await client.ping()
        await ensure_group()
        while True:
            entries = await recover_pending()
            if not entries:
                entries = await read_new()
            for stream_id, values in entries:
                await process(stream_id, values)
    finally:
        await client.aclose()
