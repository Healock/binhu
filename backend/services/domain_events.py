"""Transactional domain events backed by a MySQL outbox.

The outbox is deliberately small and contains identifiers/metadata only.  A
Relay publishes rows to Redis after the business transaction commits; a Redis
failure therefore never rolls back a successful business write.
"""

from __future__ import annotations

import json
import base64
import hashlib
import re
import uuid
from datetime import datetime
from typing import Any, Iterable


OUTBOX_TABLE = "_domain_event_outbox"
TASK_EVENT_SCHEMA_VERSION = 6
LEGACY_TASK_EVENT_SCHEMA_VERSIONS = {4}
TASK_VIEW_STATE_EVENT_SCHEMA_VERSION = 5
OUTBOX_STATUSES = {"pending", "publishing", "published", "retry", "blocked", "dead_letter"}
TASK_EVENT_CHANGED_FIELDS = {
    "address", "community", "inspector", "check_result", "task_state",
    "small_community", "person_tags", "task_graph", "online_summary",
}
TASK_EVENT_TYPES = {
    "online.task.changed", "online.task.created", "online.task.deleted",
    "online.task.claimed", "online.task.assigned", "online.task.reviewed",
    "online.task.archived",
}
TASK_VIEW_STATE_EVENT_TYPE = "online.task.view_state_changed"


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
        command_id CHAR(36) DEFAULT NULL,
        task_id VARCHAR(160) DEFAULT NULL,
        source_id BIGINT UNSIGNED DEFAULT NULL,
        operation_id CHAR(36) DEFAULT NULL,
        changed_fields_json JSON DEFAULT NULL,
        payload_envelope_json JSON DEFAULT NULL,
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
        INDEX idx_domain_event_aggregate (aggregate_type, aggregate_id, aggregate_revision),
        INDEX idx_domain_event_retention (status, published_at, event_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """


async def ensure_outbox_schema(cur) -> None:
    await cur.execute(ensure_outbox_schema_sql())


async def verify_outbox_schema(cur, *, require_task_commands: bool = False) -> None:
    required_outbox = {
        "event_id", "schema_version", "domain", "event_type", "aggregate_type",
        "aggregate_id", "aggregate_revision", "command_id", "task_id", "source_id", "operation_id",
        "changed_fields_json", "payload_envelope_json", "audiences_json", "status",
        "attempt_count", "available_at", "locked_by", "locked_until",
        "last_error_code", "last_error_summary", "occurred_at", "published_at",
    }
    await cur.execute(
        "SELECT table_name,column_name FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name IN "
        "('_domain_event_outbox','_task_command_records')"
    )
    columns: dict[str, set[str]] = {}
    for table, column in await cur.fetchall():
        columns.setdefault(str(table), set()).add(str(column))
    if not required_outbox.issubset(columns.get(OUTBOX_TABLE, set())):
        raise RuntimeError("domain_event_outbox_schema_missing")
    if require_task_commands:
        required_commands = {
            "command_id", "operation_id", "event_id", "actor_id", "environment",
            "parser_type", "task_id", "source_id", "action", "expected_revision",
            "revision", "changed_fields_json", "payload_ciphertext_sha256",
            "authorization_policy", "authorization_scope_sha256",
            "authorization_decision", "created_at",
        }
        if not required_commands.issubset(columns.get("_task_command_records", set())):
            raise RuntimeError("task_command_records_schema_missing")


def task_changed_field_categories(parser_type: str, columns: Iterable[str]) -> list[str]:
    """Map private business column names to safe, bounded event categories."""
    from services.parsers import get_parser
    from services.task_workflow import TASK_WORKFLOWS

    parser = get_parser(parser_type)
    changed = {str(column) for column in columns}
    categories: set[str] = set()
    if parser.COMMUNITY_COLUMN in changed:
        categories.add("community")
    if "核查人" in changed:
        categories.add("inspector")
    if any("地址" in column for column in changed):
        categories.add("address")
    workflow = TASK_WORKFLOWS.get(parser_type)
    if workflow and workflow.result_field in changed:
        categories.add("check_result")
    if any(any(token in column for token in ("状态", "研判", "反馈", "登记")) for column in changed):
        categories.add("task_state")
    if changed:
        categories.add("online_summary")
    if changed and not categories:
        categories.add("task_state")
    return sorted(categories)


def task_event_type_for_action(action: str) -> str:
    if action == "archive":
        return "online.task.archived"
    if action == "delete":
        return "online.task.deleted"
    if action == "claim":
        return "online.task.claimed"
    if action in {"bulk_assign_local", "bulk_unassign_local", "internal_transfer_local"}:
        return "online.task.assigned"
    if action == "review_advance":
        return "online.task.reviewed"
    return "online.task.changed"


def stable_task_id(table_name: str, physical_row: int) -> str:
    table = _safe_text(table_name, 80)
    if not table.startswith("t_") or type(physical_row) is not int or physical_row <= 0:
        raise ValueError("invalid task event identity")
    return f"{table}:{physical_row}"


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


def _validate_task_envelope(
    envelope: dict[str, str], *, command_id: str, event_id: str,
    task_id: str, revision: int,
) -> None:
    try:
        encoded_ciphertext = str(envelope["ciphertext"])
        ciphertext = base64.urlsafe_b64decode(
            encoded_ciphertext + "=" * (-len(encoded_ciphertext) % 4)
        )
        encoded_aad = str(envelope["aad"])
        aad = json.loads(base64.urlsafe_b64decode(
            encoded_aad + "=" * (-len(encoded_aad) % 4)
        ))
        expected_event_id = str(uuid.UUID(event_id))
        expected_command_id = str(uuid.UUID(command_id))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("invalid encrypted task payload") from None
    key_version = envelope.get("key_version")
    expected_contract = "binhu.task-snapshot.v3" if key_version == "task-event-v3" else "binhu.task-snapshot.v2"
    expected_fields = (
        {"contract", "environment", "task_id", "revision", "event_id", "command_id", "row_group_token"}
        if key_version == "task-event-v3"
        else {"contract", "environment", "task_id", "revision", "event_id", "command_id"}
    )
    if (
        hashlib.sha256(ciphertext).hexdigest() != envelope.get("ciphertext_sha256")
        or not isinstance(aad, dict)
        or aad.get("contract") != expected_contract
        or aad.get("environment") not in {"development", "staging", "production", "shadow"}
        or aad.get("task_id") != task_id
        or aad.get("revision") != revision
        or aad.get("event_id") != expected_event_id
        or aad.get("command_id") != expected_command_id
        or set(aad) != expected_fields
        or (key_version == "task-event-v3" and (
            not isinstance(aad.get("row_group_token"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", aad["row_group_token"])
        ))
    ):
        raise ValueError("encrypted task payload identity mismatch")


async def enqueue_event(
    cur,
    *,
    domain: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | int,
    aggregate_revision: int,
    audiences: Iterable[str],
    command_id: str | None = None,
    task_id: str | None = None,
    source_id: int | None = None,
    operation_id: str | None = None,
    changed_fields: Iterable[str] = (),
    encrypted_payload: dict[str, str] | None = None,
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
    has_task_identity = any(value is not None for value in (command_id, task_id, source_id, operation_id))
    if has_task_identity and any(value is None for value in (command_id, task_id, source_id, operation_id)):
        raise ValueError("task event identity must be complete")
    if has_task_identity:
        try:
            command_id = str(uuid.UUID(str(command_id)))
        except (ValueError, AttributeError):
            raise ValueError("invalid task event command id") from None
        task_id = _safe_text(task_id, 160)
        table, separator, physical_row = task_id.partition(":")
        if (not separator or not table.startswith("t_") or not physical_row.isdecimal()
                or int(physical_row) <= 0):
            raise ValueError("invalid task event identity")
        if domain != "online" or aggregate_type != "online_task" or event_type not in TASK_EVENT_TYPES:
            raise ValueError("invalid task domain event")
        if type(source_id) is not int or source_id <= 0:
            raise ValueError("invalid task event source id")
        if aggregate_id != task_id or aggregate_revision < 1:
            raise ValueError("task aggregate identity mismatch")
        try:
            operation_id = str(uuid.UUID(str(operation_id)))
        except (ValueError, AttributeError):
            raise ValueError("invalid task event operation id") from None
    safe_changed_fields = sorted({str(field) for field in changed_fields})
    if not set(safe_changed_fields).issubset(TASK_EVENT_CHANGED_FIELDS):
        raise ValueError("unsupported task event changed field")
    if has_task_identity and not safe_changed_fields:
        raise ValueError("task event changed fields are required")
    if encrypted_payload is not None:
        if not has_task_identity or encrypted_payload.get("algorithm") != "AES-256-GCM":
            raise ValueError("invalid encrypted task payload")
        if (encrypted_payload.get("key_version") not in {"task-event-v2", "task-event-v3"}
                or not encrypted_payload.get("nonce")
                or not encrypted_payload.get("ciphertext")
                or not encrypted_payload.get("aad")
                or not encrypted_payload.get("ciphertext_sha256")):
            raise ValueError("invalid encrypted task payload")
        _validate_task_envelope(
            encrypted_payload, command_id=str(command_id), event_id=event_id,
            task_id=str(task_id), revision=int(aggregate_revision),
        )
    audiences_json = json.dumps(_safe_audiences(audiences), ensure_ascii=False, separators=(",", ":"))
    changed_fields_json = json.dumps(safe_changed_fields, separators=(",", ":"))
    payload_json = json.dumps(encrypted_payload, separators=(",", ":")) if encrypted_payload else None
    await cur.execute(
        f"""
        INSERT INTO `{OUTBOX_TABLE}` (
            event_id, schema_version, domain, event_type, aggregate_type,
            aggregate_id, aggregate_revision, command_id, task_id, source_id, operation_id,
            changed_fields_json, payload_envelope_json, audiences_json, occurred_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  COALESCE(%s, UTC_TIMESTAMP()))
        """,
        (event_id, TASK_EVENT_SCHEMA_VERSION if has_task_identity else 1, domain, event_type, aggregate_type, aggregate_id,
         int(aggregate_revision), command_id, task_id, source_id, operation_id,
         changed_fields_json if has_task_identity else None, payload_json, audiences_json, occurred_at),
    )
    return event_id


async def enqueue_task_view_state_event(
    cur,
    *,
    environment: str,
    run_id: str,
    task_id: str,
    source_id: int,
    domain: str,
    domain_revision: int,
    task_revision: int,
    state: dict[str, Any],
    removed: bool = False,
    operation_id: str | None = None,
    occurred_at: datetime | None = None,
    outbox_table: str = OUTBOX_TABLE,
) -> str:
    """Append one domain-owned read-model state event in its source transaction."""
    from services.task_view_state import (
        TASK_VIEW_STATE_DOMAINS,
        build_task_view_state_update,
    )

    update = build_task_view_state_update(
        environment=environment,
        run_id=run_id,
        task_id=task_id,
        domain=domain,
        domain_revision=domain_revision,
        task_revision=task_revision,
        event_id=str(uuid.uuid4()),
        observed_at=(occurred_at or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        state=state,
        removed=removed,
    )
    if domain not in TASK_VIEW_STATE_DOMAINS:
        raise ValueError("unsupported task view state domain")
    if not isinstance(outbox_table, str) or not re.fullmatch(
        r"(?:[A-Za-z0-9_]+\.)?[A-Za-z0-9_]+", outbox_table,
    ):
        raise ValueError("invalid task view state outbox table")
    source_table, separator, source_row = task_id.partition(":")
    if (not separator or not source_table.startswith("t_")
            or not source_row.isdecimal() or type(source_id) is not int or source_id < 1):
        raise ValueError("invalid task view state source identity")
    try:
        operation_id = str(uuid.UUID(operation_id)) if operation_id else str(uuid.uuid4())
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid task view state operation id") from None
    metadata = json.dumps({
        "domain": domain,
        "task_revision": task_revision,
        "removed": removed,
    }, separators=(",", ":"))
    envelope_json = json.dumps(update["envelope"], separators=(",", ":"))
    event_id = update["event_id"]
    await cur.execute(
        f"""INSERT INTO {outbox_table} (
            event_id,schema_version,domain,event_type,aggregate_type,aggregate_id,
            aggregate_revision,command_id,task_id,source_id,operation_id,
            changed_fields_json,payload_envelope_json,audiences_json,occurred_at
        ) VALUES (%s,%s,'online',%s,'task_view_state',%s,%s,NULL,%s,%s,%s,%s,%s,%s,
                  COALESCE(%s,UTC_TIMESTAMP()))""",
        (
            event_id, TASK_VIEW_STATE_EVENT_SCHEMA_VERSION,
            TASK_VIEW_STATE_EVENT_TYPE, task_id, domain_revision, task_id,
            source_id, operation_id, metadata, envelope_json,
            json.dumps(["authenticated"], separators=(",", ":")), occurred_at,
        ),
    )
    return event_id


def decode_event_row(row: Any) -> dict[str, Any]:
    """Convert a DB tuple/dict to a safe Redis event envelope."""
    if isinstance(row, dict):
        get = row.get
    else:
        legacy_names = (
            "event_id", "schema_version", "domain", "event_type",
            "aggregate_type", "aggregate_id", "aggregate_revision",
            "audiences_json", "status", "attempt_count", "available_at",
            "locked_by", "locked_until", "last_error_code",
            "last_error_summary", "occurred_at", "published_at",
        )
        current_names = (
            "event_id", "schema_version", "domain", "event_type",
            "aggregate_type", "aggregate_id", "aggregate_revision",
            "task_id", "source_id", "operation_id", "changed_fields_json",
            "payload_envelope_json", "audiences_json", "status", "attempt_count", "available_at",
            "locked_by", "locked_until", "last_error_code",
            "last_error_summary", "occurred_at", "published_at",
        )
        if len(row) <= len(legacy_names):
            names = legacy_names
        elif len(row) <= len(current_names):
            names = current_names
        else:
            names = (
                "event_id", "schema_version", "domain", "event_type",
                "aggregate_type", "aggregate_id", "aggregate_revision", "command_id",
                "task_id", "source_id", "operation_id", "changed_fields_json",
                "payload_envelope_json", "audiences_json", "status", "attempt_count", "available_at",
                "locked_by", "locked_until", "last_error_code",
                "last_error_summary", "occurred_at", "published_at",
            )
        get = lambda key: (
            row[names.index(key)]
            if key in names and names.index(key) < len(row) else None
        )
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
    command_id = get("command_id")
    view_state_metadata = None
    if schema_version == TASK_VIEW_STATE_EVENT_SCHEMA_VERSION:
        from services.task_view_state import TASK_VIEW_STATE_DOMAINS, task_view_state_keys

        if isinstance(changed_fields, dict):
            view_state_metadata = changed_fields
        if (get("domain") != "online"
                or get("event_type") != TASK_VIEW_STATE_EVENT_TYPE
                or get("aggregate_type") != "task_view_state"
                or not isinstance(view_state_metadata, dict)
                or set(view_state_metadata) != {"domain", "task_revision", "removed"}
                or view_state_metadata.get("domain") not in TASK_VIEW_STATE_DOMAINS
                or type(view_state_metadata.get("task_revision")) is not int
                or view_state_metadata["task_revision"] < 1
                or type(view_state_metadata.get("removed")) is not bool
                or not isinstance(encrypted_payload, dict)
                or encrypted_payload.get("algorithm") != "AES-256-GCM"
                or encrypted_payload.get("key_version") != "task-view-state-v1"):
            raise ValueError("task view state event contract invalid")
        try:
            event_id = str(uuid.UUID(str(get("event_id"))))
            operation_id = str(uuid.UUID(str(get("operation_id"))))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("task view state event identity invalid") from None
        task_id = str(get("task_id") or "")
        task_view_state_keys(
            "development", "dev-event-validation", task_id,
            view_state_metadata["domain"],
        )
        if type(get("source_id")) is not int or get("source_id") < 1:
            raise ValueError("task view state source identity invalid")
        if type(get("aggregate_revision")) is not int or get("aggregate_revision") < 1:
            raise ValueError("task view state revision invalid")
    if schema_version in (*LEGACY_TASK_EVENT_SCHEMA_VERSIONS, TASK_EVENT_SCHEMA_VERSION):
        try:
            command_id = str(uuid.UUID(str(command_id)))
            event_id = str(uuid.UUID(str(get("event_id"))))
            operation_id = str(uuid.UUID(str(get("operation_id"))))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("task event identity invalid") from None
        deleted = get("event_type") == "online.task.deleted"
        if (not get("task_id") or type(get("source_id")) is not int or get("source_id") <= 0
                or not isinstance(changed_fields, list)
                or not changed_fields
                or not set(changed_fields).issubset(TASK_EVENT_CHANGED_FIELDS)
                or get("domain") != "online"
                or get("aggregate_type") != "online_task"
                or get("event_type") not in TASK_EVENT_TYPES
                or str(get("aggregate_id") or "") != str(get("task_id") or "")
                or int(get("aggregate_revision") or 0) < 1):
            raise ValueError("task event v4 contract invalid")
        if deleted:
            if encrypted_payload is not None:
                raise ValueError("task deletion payload must be empty")
        else:
            if (not isinstance(encrypted_payload, dict)
                    or encrypted_payload.get("algorithm") != "AES-256-GCM"
                    or encrypted_payload.get("key_version") != (
                        "task-event-v3" if schema_version == TASK_EVENT_SCHEMA_VERSION
                        else "task-event-v2"
                    )):
                raise ValueError("task event encrypted payload required")
            _validate_task_envelope(
                encrypted_payload,
                command_id=command_id,
                event_id=event_id,
                task_id=str(get("task_id")),
                revision=int(get("aggregate_revision")),
            )
    event = {
        "event_id": str(get("event_id")),
        "schema_version": schema_version,
        "domain": str(get("domain") or ""),
        "event_type": str(get("event_type") or ""),
        "aggregate_type": str(get("aggregate_type") or ""),
        "aggregate_id": str(get("aggregate_id") or ""),
        "aggregate_revision": int(get("aggregate_revision") or 0),
        "command_id": str(command_id) if command_id else None,
        "task_id": str(get("task_id") or ""),
        "source_id": int(get("source_id")) if get("source_id") is not None else None,
        "operation_id": str(get("operation_id") or ""),
        "changed_fields": (
            ["task_state"] if schema_version == TASK_VIEW_STATE_EVENT_SCHEMA_VERSION
            else [str(item) for item in changed_fields] if isinstance(changed_fields, list)
            else []
        ),
        "encrypted_payload": encrypted_payload if isinstance(encrypted_payload, dict) else None,
        "view_state": view_state_metadata,
        "audiences": [str(item) for item in audiences],
        "occurred_at": (get("occurred_at").isoformat() if get("occurred_at") else None),
    }
    if schema_version == TASK_EVENT_SCHEMA_VERSION and isinstance(encrypted_payload, dict):
        aad = json.loads(base64.urlsafe_b64decode(
            encrypted_payload["aad"] + "=" * (-len(encrypted_payload["aad"]) % 4)
        ))
        event["row_group_token"] = aad["row_group_token"]
    return event
