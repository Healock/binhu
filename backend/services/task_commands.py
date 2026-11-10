"""Shared command and encrypted snapshot contract for online tasks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


COMMAND_TABLE = "_task_command_records"
COMMAND_ACTIONS = {
    "create", "edit", "delete", "claim", "unclaim", "assign", "unassign",
    "address_confirm", "address_conflict_resolve", "review_initial",
    "review_deep", "registration_confirm", "internal_transfer", "archive",
    "restore",
}


@dataclass(frozen=True)
class TaskCommandSpec:
    actor_id: int
    environment: str
    parser_type: str
    task_id: str | None
    source_id: int | None
    action: str
    event_type: str
    expected_revision: int | None
    audiences: tuple[str, ...]


@dataclass(frozen=True)
class TaskAuthorizationResult:
    policy_key: str
    scope_values: tuple[str, ...] = ()
    decision: str = "allow"


@dataclass(frozen=True)
class TaskMutationResult:
    revision: int
    snapshot: dict[str, Any]
    changed_fields: tuple[str, ...]
    result: Any = None
    operation_id: str | None = None
    task_id: str | None = None
    source_id: int | None = None


def _expected_event_type(action: str) -> str:
    if action == "create":
        return "online.task.created"
    if action == "delete":
        return "online.task.deleted"
    if action == "archive":
        return "online.task.archived"
    if action == "claim":
        return "online.task.claimed"
    if action in {"assign", "unassign", "internal_transfer"}:
        return "online.task.assigned"
    if action in {"review_initial", "review_deep"}:
        return "online.task.reviewed"
    return "online.task.changed"


def validate_task_command_transition(
    *, action: str, event_type: str, expected_revision: int | None,
    revision: int,
) -> None:
    if action not in COMMAND_ACTIONS:
        raise TaskCommandContractError("unregistered task command action")
    if event_type != _expected_event_type(action):
        raise TaskCommandContractError("task command event does not match its action")
    if action == "create":
        if expected_revision is not None or revision != 1:
            raise TaskCommandContractError("invalid task creation revision")
        return
    if (type(expected_revision) is not int or expected_revision < 1
            or type(revision) is not int or revision != expected_revision + 1):
        raise TaskCommandContractError("invalid task command revision transition")


async def execute_task_command(
    cur,
    *,
    spec: TaskCommandSpec,
    authorize: Callable[
        [Any, dict[str, Any] | None], Awaitable[TaskAuthorizationResult]
    ],
    mutate: Callable[[Any, dict[str, Any] | None], Awaitable[TaskMutationResult]],
) -> TaskMutationResult:
    """Run policy validation, authoritative mutation, and event writes together.

    The caller owns the surrounding MySQL transaction.  The authorizer and
    mutation callback must use that cursor so a rejected command or failed
    Outbox write rolls the complete operation back.
    """
    if not callable(authorize) or not callable(mutate):
        raise TaskCommandContractError("task command requires authorization and mutation handlers")
    if (
        type(spec.actor_id) is not int or spec.actor_id < 0
        or spec.environment not in {"development", "staging", "production", "shadow"}
        or not spec.parser_type or len(spec.parser_type) > 80
    ):
        raise TaskCommandContractError("invalid task command actor or environment")
    if spec.action not in COMMAND_ACTIONS:
        raise TaskCommandContractError("unregistered task command action")
    if spec.event_type != _expected_event_type(spec.action):
        raise TaskCommandContractError("task command event does not match its action")
    current_source = None
    if spec.action != "create":
        if (type(spec.source_id) is not int or spec.source_id <= 0
                or not spec.task_id or spec.expected_revision is None):
            raise TaskCommandContractError("task command source identity is required")
        await cur.execute(
            "SELECT parser_type,physical_row,revision,archived_at,values_json "
            "FROM _online_source_rows WHERE id=%s FOR UPDATE",
            (spec.source_id,),
        )
        source_row = await cur.fetchone()
        if not source_row or source_row[3] is not None:
                raise TaskCommandSourceUnavailable("task command source is unavailable")
        parser_type = str(source_row[0] or "")
        physical_row = int(source_row[1] or 0)
        current_revision = int(source_row[2] or 0)
        if parser_type != spec.parser_type or current_revision != spec.expected_revision:
            raise TaskCommandRevisionConflict("task command source revision conflict")
        from services.parsers import get_parser

        parser = get_parser(parser_type)
        if spec.task_id != f"{parser.table_name}:{physical_row}":
            raise TaskCommandContractError("task command source identity mismatch")
        raw_values = source_row[4]
        if isinstance(raw_values, (str, bytes)):
            raw_values = json.loads(raw_values or "{}")
        current_source = {
            "parser_type": parser_type,
            "physical_row": physical_row,
            "revision": current_revision,
            "values": raw_values if isinstance(raw_values, dict) else {},
        }
    authorization = await authorize(cur, current_source)
    if (
        not isinstance(authorization, TaskAuthorizationResult)
        or authorization.decision != "allow"
        or not isinstance(authorization.policy_key, str)
        or not authorization.policy_key
        or len(authorization.policy_key) > 80
        or not isinstance(authorization.scope_values, (tuple, list, set, frozenset))
        or any(not isinstance(value, str) or not value for value in authorization.scope_values)
    ):
        raise TaskCommandContractError("task command authorization result is invalid")
    outcome = await mutate(cur, current_source)
    if not isinstance(outcome, TaskMutationResult):
        raise TaskCommandContractError("task mutation returned an invalid result")
    task_id = outcome.task_id or spec.task_id
    source_id = outcome.source_id or spec.source_id
    if type(source_id) is not int or source_id <= 0 or not task_id:
        raise TaskCommandContractError("task mutation returned an invalid identity")
    validate_task_command_transition(
        action=spec.action,
        event_type=spec.event_type,
        expected_revision=spec.expected_revision,
        revision=outcome.revision,
    )
    await cur.execute(
        "SELECT parser_type,physical_row,revision,archived_at "
        "FROM _online_source_rows WHERE id=%s",
        (source_id,),
    )
    persisted_source = await cur.fetchone()
    if spec.action == "create":
        from services.parsers import get_parser

        parser = get_parser(spec.parser_type)
        if (
            not persisted_source
            or str(persisted_source[0] or "") != spec.parser_type
            or int(persisted_source[2] or 0) != outcome.revision
            or persisted_source[3] is not None
            or task_id != f"{parser.table_name}:{int(persisted_source[1] or 0)}"
        ):
            raise TaskCommandContractError("task creation mutation was not persisted")
    elif spec.action in {"delete", "archive"}:
        if persisted_source and (
            str(persisted_source[0] or "") != spec.parser_type
            or int(persisted_source[2] or 0) != outcome.revision
            or persisted_source[3] is None
        ):
            raise TaskCommandContractError("task archival mutation was not persisted")
    elif (
        not persisted_source
        or str(persisted_source[0] or "") != spec.parser_type
        or int(persisted_source[2] or 0) != outcome.revision
        or persisted_source[3] is not None
    ):
        raise TaskCommandContractError("task mutation revision was not persisted")
    await _append_command_and_outbox(
        cur,
        actor_id=spec.actor_id,
        environment=spec.environment,
        parser_type=spec.parser_type,
        task_id=task_id,
        source_id=source_id,
        action=spec.action,
        event_type=spec.event_type,
        expected_revision=spec.expected_revision,
        revision=outcome.revision,
        changed_fields=outcome.changed_fields,
        snapshot=outcome.snapshot,
        audiences=spec.audiences,
        operation_id=outcome.operation_id,
        authorization=authorization,
    )
    from services.task_view_state import enqueue_current_task_view_state_snapshot

    await enqueue_current_task_view_state_snapshot(
        cur,
        source_id=source_id,
        snapshot=outcome.snapshot,
        removed=spec.action in {"delete", "archive"},
    )
    return outcome

def normalize_action(value: str) -> str:
    action = str(value or "").lower()
    aliases = {
        "edit": "edit",
        "update": "edit",
        "local_update": "edit",
        "system_local_update": "edit",
        "auto_registration": "registration_confirm",
        "manual_registration": "registration_confirm",
        "claim": "claim",
        "unclaim": "unclaim",
        "assign": "assign",
        "bulk_assign_local": "assign",
        "unassign": "unassign",
        "bulk_unassign_local": "unassign",
        "address_confirm": "address_confirm",
        "address_match.resolve_conflict": "address_conflict_resolve",
        "address_conflict_resolve": "address_conflict_resolve",
        "review_initial": "review_initial",
        "review_deep": "review_deep",
        "review_advance": "review_deep",
        "unverifiable_review_decision": "review_deep",
        "registration_confirm": "registration_confirm",
        "internal_transfer": "internal_transfer",
        "internal_transfer_local": "internal_transfer",
        "archive": "archive",
        "restore": "restore",
        "delete": "delete",
        "create": "create",
    }
    if action in aliases:
        return aliases[action]
    if "conflict" in action and "address" in action:
        return "address_conflict_resolve"
    if "review_initial" in action or "initial_review" in action or "初步" in action:
        return "review_initial"
    if "review" in action:
        return "review_initial" if "initial" in action or "初步" in action else "review_deep"
    for token, normalized in (
        ("delete", "delete"), ("archive", "archive"), ("restore", "restore"),
        ("transfer", "internal_transfer"), ("review", "review_deep"),
        ("registration", "registration_confirm"), ("address", "address_confirm"),
        ("unclaim", "unclaim"), ("claim", "claim"),
        ("unassign", "unassign"), ("assign", "assign"),
        ("create", "create"),
    ):
        if token in action:
            return normalized
    raise TaskCommandContractError("unregistered task command action")


class TaskCommandContractError(ValueError):
    pass


class TaskCommandRevisionConflict(TaskCommandContractError):
    pass


class TaskCommandSourceUnavailable(TaskCommandContractError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _task_event_key() -> bytes:
    from config import settings

    secret_value = str(settings.TASK_EVENT_ENCRYPTION_KEY or "")
    if len(secret_value) < 32:
        raise TaskCommandContractError("dedicated task event encryption key is required")
    secret = secret_value.encode("utf-8")
    return hashlib.sha256(b"binhu/task-event/aes-gcm/v1\0" + secret).digest()


def task_row_group_token(
    snapshot: dict[str, Any], *, parser_type: str, environment: str,
) -> str:
    """Return an environment-scoped opaque identity for source-row grouping."""
    if environment not in {"development", "staging", "production", "shadow"}:
        raise TaskCommandContractError("invalid task event environment")
    if not isinstance(parser_type, str) or not parser_type or len(parser_type) > 80:
        raise TaskCommandContractError("invalid task parser identity")
    from services.parsers import get_parser

    try:
        row_key = get_parser(parser_type).make_row_key(snapshot)
    except Exception as exc:
        raise TaskCommandContractError("task row identity unavailable") from exc
    if not isinstance(row_key, str) or not row_key or len(row_key) > 200:
        raise TaskCommandContractError("task row identity unavailable")
    environment_key = hmac.new(
        _task_event_key(),
        b"binhu/task-read-model/group-key/v1\0" + environment.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    digest = hmac.new(
        environment_key,
        parser_type.encode("utf-8") + b"\0" + row_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest


def encrypt_task_snapshot(
    snapshot: dict[str, Any], *, environment: str, task_id: str, revision: int,
    event_id: str, command_id: str | None = None,
    row_group_token: str | None = None,
) -> dict[str, str]:
    if not isinstance(snapshot, dict) or not snapshot:
        raise TaskCommandContractError("task snapshot is required")
    if environment not in {"development", "staging", "production", "shadow"}:
        raise TaskCommandContractError("invalid task event environment")
    if not task_id or len(task_id) > 160 or revision < 1:
        raise TaskCommandContractError("invalid task event identity")
    try:
        normalized_event_id = str(uuid.UUID(event_id))
    except (ValueError, TypeError, AttributeError):
        raise TaskCommandContractError("invalid task event id") from None
    if command_id is not None:
        try:
            command_id = str(uuid.UUID(command_id))
        except (ValueError, TypeError, AttributeError):
            raise TaskCommandContractError("invalid task command id") from None
    if row_group_token is not None and not re.fullmatch(r"[0-9a-f]{64}", row_group_token):
        raise TaskCommandContractError("invalid task row group token")

    nonce = os.urandom(12)
    identity = {
        "contract": (
            "binhu.task-snapshot.v3" if row_group_token is not None
            else "binhu.task-snapshot.v2" if command_id
            else "binhu.task-snapshot.v1"
        ),
        "environment": environment,
        "task_id": task_id,
        "revision": revision,
        "event_id": normalized_event_id,
    }
    if command_id:
        identity["command_id"] = command_id
    if row_group_token is not None:
        identity["row_group_token"] = row_group_token
    associated_data = canonical_json(identity)
    ciphertext = AESGCM(_task_event_key()).encrypt(
        nonce, canonical_json(snapshot), associated_data,
    )
    return {
        "algorithm": "AES-256-GCM",
        "key_version": (
            "task-event-v3" if row_group_token is not None
            else "task-event-v2" if command_id
            else "task-event-v1"
        ),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii").rstrip("="),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii").rstrip("="),
        "aad": base64.urlsafe_b64encode(associated_data).decode("ascii").rstrip("="),
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
    }


def decrypt_task_snapshot(
    envelope: dict[str, str], *, environment: str | None = None,
    task_id: str | None = None, revision: int | None = None,
    event_id: str | None = None, command_id: str | None = None,
    row_group_token: str | None = None,
) -> dict[str, Any]:
    key_version = envelope.get("key_version")
    if envelope.get("algorithm") != "AES-256-GCM" or key_version not in {
        "task-event-v1", "task-event-v2", "task-event-v3",
    }:
        raise TaskCommandContractError("unsupported task event encryption")

    def decode(name: str) -> bytes:
        value = str(envelope.get(name) or "")
        if not value:
            raise TaskCommandContractError("invalid task event encryption")
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    try:
        ciphertext = decode("ciphertext")
        if hashlib.sha256(ciphertext).hexdigest() != envelope.get("ciphertext_sha256"):
            raise TaskCommandContractError("task event ciphertext hash mismatch")
        if any(value is not None for value in (environment, task_id, revision, event_id, command_id)):
            if any(value is None for value in (environment, task_id, revision, event_id)):
                raise TaskCommandContractError("task event identity must be complete")
            normalized_command_id = str(uuid.UUID(str(command_id))) if command_id is not None else None
            aad_value = json.loads(decode("aad"))
            if row_group_token is None and key_version == "task-event-v3":
                row_group_token = aad_value.get("row_group_token")
            expected_version = (
                "task-event-v3" if row_group_token is not None
                else "task-event-v2" if normalized_command_id
                else "task-event-v1"
            )
            if key_version != expected_version:
                raise TaskCommandContractError("task event command identity mismatch")
            if row_group_token is not None and not re.fullmatch(r"[0-9a-f]{64}", row_group_token):
                raise TaskCommandContractError("invalid task row group token")
            identity = {
                "contract": (
                    "binhu.task-snapshot.v3" if row_group_token is not None
                    else "binhu.task-snapshot.v2" if normalized_command_id
                    else "binhu.task-snapshot.v1"
                ),
                "environment": environment,
                "task_id": task_id,
                "revision": revision,
                "event_id": str(uuid.UUID(str(event_id))),
            }
            if normalized_command_id:
                identity["command_id"] = normalized_command_id
            if row_group_token is not None:
                identity["row_group_token"] = row_group_token
            expected_aad = canonical_json(identity)
            if decode("aad") != expected_aad:
                raise TaskCommandContractError("task event identity mismatch")
        plaintext = AESGCM(_task_event_key()).decrypt(
            decode("nonce"), ciphertext, decode("aad"),
        )
        value = json.loads(plaintext)
    except TaskCommandContractError:
        raise
    except Exception as exc:
        raise TaskCommandContractError("task event decryption failed") from exc
    if not isinstance(value, dict):
        raise TaskCommandContractError("task event snapshot is invalid")
    return value


def normalize_changed_fields(values: Iterable[str]) -> list[str]:
    allowed = {
        "address", "community", "inspector", "check_result", "task_state",
        "small_community", "person_tags", "task_graph", "online_summary",
    }
    fields = sorted({str(value) for value in values})
    if not fields or not set(fields).issubset(allowed):
        raise TaskCommandContractError("invalid task command field categories")
    return fields


async def append_task_command(
    cur,
    *,
    command_id: str,
    operation_id: str,
    actor_id: int,
    environment: str,
    parser_type: str,
    task_id: str,
    source_id: int,
    action: str,
    expected_revision: int | None,
    revision: int,
    changed_fields: Iterable[str],
    snapshot: dict[str, Any],
    event_id: str,
    authorization_policy: str,
    authorization_scope_sha256: str,
    authorization_decision: str,
) -> dict[str, str]:
    """Append immutable command metadata in the caller's business transaction."""
    try:
        command_id = str(uuid.UUID(command_id))
        operation_id = str(uuid.UUID(operation_id))
        event_id = str(uuid.UUID(event_id))
    except (ValueError, TypeError, AttributeError):
        raise TaskCommandContractError("invalid task command identity") from None
    fields = normalize_changed_fields(changed_fields)
    if (
        action not in COMMAND_ACTIONS or actor_id < 0 or source_id <= 0
        or revision < 1 or (expected_revision is not None and expected_revision < 0)
        or not parser_type or len(parser_type) > 80 or not task_id or len(task_id) > 160
        or not authorization_policy or len(authorization_policy) > 80
        or len(authorization_scope_sha256) != 64
        or authorization_decision != "allow"
    ):
        raise TaskCommandContractError("invalid task command")

    row_group_token = task_row_group_token(
        snapshot, parser_type=parser_type, environment=environment,
    )
    envelope = encrypt_task_snapshot(
        snapshot, environment=environment, task_id=task_id,
        revision=revision, event_id=event_id, command_id=command_id,
        row_group_token=row_group_token,
    )
    metadata = {
        "command_id": command_id,
        "operation_id": operation_id,
        "event_id": event_id,
        "actor_id": actor_id,
        "environment": environment,
        "parser_type": parser_type,
        "task_id": task_id,
        "source_id": source_id,
        "action": action,
        "expected_revision": expected_revision,
        "revision": revision,
        "changed_fields": fields,
        "payload_ciphertext_sha256": envelope["ciphertext_sha256"],
    }
    await cur.execute(
        f"""INSERT INTO `{COMMAND_TABLE}`
            (command_id,operation_id,event_id,actor_id,environment,parser_type,
             task_id,source_id,action,expected_revision,revision,changed_fields_json,
             payload_ciphertext_sha256,authorization_policy,
             authorization_scope_sha256,authorization_decision,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())""",
        (
            command_id, operation_id, event_id, actor_id, environment, parser_type,
            task_id, source_id, action, expected_revision, revision,
            json.dumps(fields, separators=(",", ":")), envelope["ciphertext_sha256"],
            authorization_policy, authorization_scope_sha256, authorization_decision,
        ),
    )
    return envelope


async def _append_command_and_outbox(
    cur,
    *,
    actor_id: int,
    environment: str,
    parser_type: str,
    task_id: str,
    source_id: int,
    action: str,
    event_type: str,
    expected_revision: int | None,
    revision: int,
    changed_fields: Iterable[str],
    snapshot: dict[str, Any],
    audiences: Iterable[str],
    operation_id: str | None = None,
    authorization: TaskAuthorizationResult,
    command_id: str | None = None,
) -> tuple[str, str, str]:
    """Write the command ledger and outbox event in the caller's transaction."""
    validate_task_command_transition(
        action=action,
        event_type=event_type,
        expected_revision=expected_revision,
        revision=revision,
    )
    command_id = str(uuid.uuid4()) if command_id is None else str(uuid.UUID(command_id))
    operation_id = operation_id or str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    if (
        not isinstance(authorization, TaskAuthorizationResult)
        or authorization.decision != "allow"
        or not authorization.policy_key
    ):
        raise TaskCommandContractError("task command authorization result is invalid")
    policy_key = authorization.policy_key
    scope_hash = hashlib.sha256(
        b"binhu/task-command-scope/v1\0"
        + canonical_json(sorted(set(authorization.scope_values)))
    ).hexdigest()
    decision = authorization.decision
    envelope = await append_task_command(
        cur,
        command_id=command_id,
        operation_id=operation_id,
        actor_id=actor_id,
        environment=environment,
        parser_type=parser_type,
        task_id=task_id,
        source_id=source_id,
        action=action,
        expected_revision=expected_revision,
        revision=revision,
        changed_fields=changed_fields,
        snapshot=snapshot,
        event_id=event_id,
        authorization_policy=policy_key,
        authorization_scope_sha256=scope_hash,
        authorization_decision=decision,
    )
    from services.domain_events import enqueue_event

    await enqueue_event(
        cur,
        domain="online",
        event_type=event_type,
        aggregate_type="online_task",
        aggregate_id=task_id,
        aggregate_revision=revision,
        audiences=audiences,
        command_id=command_id,
        task_id=task_id,
        source_id=source_id,
        operation_id=operation_id,
        changed_fields=normalize_changed_fields(changed_fields),
        event_id=event_id,
        encrypted_payload=None if event_type == "online.task.deleted" else envelope,
    )
    return command_id, operation_id, event_id


class TaskCommandService:
    execute = staticmethod(execute_task_command)
    normalize_action = staticmethod(normalize_action)
