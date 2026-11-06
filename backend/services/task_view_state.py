"""Versioned auxiliary state contract for the task read model."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.task_commands import TaskCommandContractError, canonical_json


TASK_VIEW_STATE_DOMAINS = frozenset({
    "address_match", "watch", "qmf", "residence", "registration", "review",
    "task_graph",
})
TASK_VIEW_STATE_INDEX_VERSION = "v1"

_STATE_INDEX_ENUMS = {
    ("address_match", "status"): frozenset({
        "unmatched", "suggested", "ambiguous", "conflict", "invalid",
        "confirmed", "manual_unmatched", "review_required",
    }),
    ("qmf", "state"): frozenset({
        "not_applicable", "not_scanned", "stale", "error", "pending",
        "completed_match", "completed_mismatch", "not_found", "non_jurisdiction",
        "ambiguous", "station_mismatch", "unknown_result", "unavailable",
    }),
    ("residence", "state"): frozenset({
        "not_applicable", "pending", "querying", "registered", "cancelled",
        "legacy_completed", "confirmed", "error",
    }),
    ("registration", "status"): frozenset({
        "not_applicable", "awaiting_match", "matched_once", "review_required",
        "confirmation_pending", "confirmed", "legacy_completed",
        "pending_establishment",
    }),
    ("review", "state"): frozenset({
        "not_applicable", "initial_pending", "initial_extension", "deep_pending",
        "deep_extension", "final_unverifiable", "resolved", "archived",
        "source_exception", "new_clue", "no_new_clue",
    }),
    ("task_graph", "state"): frozenset({
        "not_applicable", "blocked", "ready",
    }),
}
_STATE_INDEX_FIELDS = {
    "address_match": frozenset({"status", "small_community_name"}),
    "watch": frozenset({"category_id"}),
    "qmf": frozenset({"state"}),
    "residence": frozenset({"state"}),
    "registration": frozenset({"status"}),
    "review": frozenset({"state"}),
    "task_graph": frozenset({"state", "blocked"}),
}


def task_view_state_events_enabled() -> bool:
    from config import settings

    return bool(
        settings.APP_ENVIRONMENT in {"development", "staging"}
        and str(settings.TASK_READ_MODEL_RUN_ID or "").strip()
    )
_TASK_ID = re.compile(r"^(t_[a-z0-9_]+):([1-9][0-9]{0,18})$")
_RUN_ID = re.compile(r"^(?:dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}|STG-[0-9]{8}-[0-9]{2})$")

TASK_VIEW_STATE_REVISIONS_SQL = """
CREATE TABLE IF NOT EXISTS `_task_view_state_revisions` (
    source_id BIGINT UNSIGNED NOT NULL,
    domain VARCHAR(32) NOT NULL,
    domain_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    task_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    state_fingerprint CHAR(64) NOT NULL DEFAULT '',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id,domain),
    INDEX idx_task_view_state_revision (domain,domain_revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def task_view_state_keys(
    environment: str, run_id: str, task_id: str, domain: str,
) -> tuple[str, str, str]:
    if environment not in {"development", "staging", "production", "shadow"}:
        raise TaskCommandContractError("invalid task view state environment")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise TaskCommandContractError("invalid task view state run")
    match = _TASK_ID.fullmatch(task_id) if isinstance(task_id, str) else None
    if match is None or match.group(1) not in {
        "t_fullchain", "t_rental_check", "t_police_stats", "t_suspect_unrevoked",
        "t_suspect_return", "t_delivery_industry", "t_group_rental",
        "t_suzhou_police", "t_traffic_police",
    }:
        raise TaskCommandContractError("invalid task view state identity")
    if domain not in TASK_VIEW_STATE_DOMAINS:
        raise TaskCommandContractError("unsupported task view state domain")
    prefix = f"binhu:task-rm:v1:{environment}:{run_id}:view-state:{task_id}:{domain}"
    return f"{prefix}:record", f"{prefix}:index:v1:members", prefix


def _state_index_key(
    *, environment: str, run_id: str, domain: str, field: str,
    value: Any,
) -> str:
    task_view_state_keys(
        environment, run_id, "t_fullchain:1", domain,
    )
    if field not in _STATE_INDEX_FIELDS[domain]:
        raise TaskCommandContractError("unsupported task view state index field")
    if (domain, field) in _STATE_INDEX_ENUMS:
        if not isinstance(value, str) or value not in _STATE_INDEX_ENUMS[(domain, field)]:
            raise TaskCommandContractError("unsupported task view state index value")
        normalized = value
    elif (domain, field) == ("watch", "category_id"):
        if type(value) is not int or value < 1:
            raise TaskCommandContractError("invalid task view state category index")
        normalized = str(value)
    elif (domain, field) == ("task_graph", "blocked"):
        if type(value) is not bool:
            raise TaskCommandContractError("invalid task graph blocked index")
        normalized = "true" if value else "false"
    elif (domain, field) == ("address_match", "small_community_name"):
        if not isinstance(value, str):
            raise TaskCommandContractError("invalid small community index")
        normalized = re.sub(r"\s+", " ", value.strip()).casefold()
        if not normalized or len(normalized) > 256:
            raise TaskCommandContractError("invalid small community index")
    else:
        raise TaskCommandContractError("unsupported task view state index field")
    from config import settings
    from services.task_read_model import search_token

    secret = str(settings.TASK_EVENT_ENCRYPTION_KEY or "")
    token = search_token(
        normalized, field=f"state:{domain}:{field}",
        environment=environment, secret=secret,
    )
    prefix = f"binhu:task-rm:v4:{environment}:{run_id}"
    return f"{prefix}:stateidx:v1:{token}"


def task_view_state_index_keys(
    *, environment: str, run_id: str, domain: str,
    state: dict[str, Any], removed: bool = False,
) -> tuple[str, ...]:
    """Return environment/run-scoped HMAC index keys for allowlisted state facets."""
    if not isinstance(state, dict) or type(removed) is not bool:
        raise TaskCommandContractError("invalid task view state index payload")
    if removed:
        if state:
            raise TaskCommandContractError("removed task view state must be empty")
        return ()
    task_view_state_keys(environment, run_id, "t_fullchain:1", domain)
    pairs: set[tuple[str, str, Any]] = set()
    if domain == "address_match":
        status = state.get("status", "unmatched")
        pairs.add((domain, "status", status))
        candidate = state.get("candidate")
        if isinstance(candidate, dict):
            name = candidate.get("name") or candidate.get("community_name")
            if name:
                pairs.add((domain, "small_community_name", name))
    elif domain == "watch":
        marks = state.get("watch_marks", [])
        if not isinstance(marks, list):
            raise TaskCommandContractError("invalid watch category index payload")
        for mark in marks:
            if not isinstance(mark, dict):
                raise TaskCommandContractError("invalid watch category index payload")
            pairs.add((domain, "category_id", mark.get("category_id")))
    elif domain in {"qmf", "residence"}:
        pairs.add((domain, "state", state.get("state", state.get("status"))))
    elif domain == "registration":
        pairs.add((domain, "status", state.get("status", "not_applicable")))
    elif domain == "review":
        pairs.add((domain, "state", state.get("state", "not_applicable")))
    elif domain == "task_graph":
        pairs.add((domain, "state", state.get("state", "not_applicable")))
        pairs.add((domain, "blocked", state.get("blocked", False)))
    else:
        raise TaskCommandContractError("unsupported task view state index domain")
    return tuple(sorted({
        _state_index_key(
            environment=environment, run_id=run_id, domain=item_domain,
            field=field, value=value,
        )
        for item_domain, field, value in pairs
        if not (field == "small_community_name" and not str(value or "").strip())
    }))


async def enqueue_source_task_view_state(
    cur, *, source_id: int, domain: str, state: dict[str, Any],
    removed: bool = False, operation_id: str | None = None,
    include_archived: bool = False,
) -> str | None:
    """Allocate a domain revision and append its encrypted event in this DB transaction."""
    from config import settings
    from services.domain_events import enqueue_task_view_state_event

    if type(source_id) is not int or source_id < 1 or domain not in TASK_VIEW_STATE_DOMAINS:
        raise TaskCommandContractError("invalid task view state source")
    if settings.APP_ENVIRONMENT not in {"development", "staging"}:
        raise TaskCommandContractError("task view state pipeline is not enabled here")
    online_db = str(settings.MYSQL_ONLINE_DATA_DB or "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", online_db):
        raise TaskCommandContractError("task view state database identity invalid")
    online = f"`{online_db}`."
    await cur.execute(
        f"SELECT parser_type,physical_row,revision,row_hash,archived_at,row_key "
        f"FROM {online}`_online_source_rows` "
        "WHERE id=%s " + ("FOR UPDATE" if include_archived else "AND archived_at IS NULL FOR UPDATE"),
        (source_id,),
    )
    source = await cur.fetchone()
    if not source or int(source[1] or 0) < 1 or int(source[2] or 0) < 1:
        raise TaskCommandContractError("task view state source unavailable")
    if source[4] is not None and not include_archived:
        raise TaskCommandContractError("task view state source unavailable")
    from services.parsers import get_parser

    task_id = f"{get_parser(str(source[0])).table_name}:{int(source[1])}"
    row_key = str(source[5] or "")
    if not removed and domain in {"qmf", "residence"}:
        state = dict(state)
        state.setdefault("state", state.get("status") or "not_scanned")
        state.setdefault("status", state["state"])
    if domain == "address_match" and not removed:
        await cur.execute(
            f"SELECT reason_code,created_at,source_id,source_revision "
            f"FROM {online}`_online_task_address_unmatched_events` "
            "WHERE parser_type=%s AND row_key=%s ORDER BY id DESC LIMIT 20",
            (str(source[0]), row_key),
        )
        state = dict(state)
        state["history"] = [
            {
                "reason_code": str(item[0] or ""),
                "created_at": item[1].isoformat() + "Z" if hasattr(item[1], "isoformat") else str(item[1] or ""),
                "source_id": int(item[2]) if item[2] is not None else None,
                "source_revision": int(item[3]) if item[3] is not None else None,
            }
            for item in await cur.fetchall()
        ]
    await cur.execute(
        f"INSERT IGNORE INTO {online}`_task_view_state_revisions` "
        "(source_id,domain,domain_revision,task_revision) VALUES (%s,%s,0,0)",
        (source_id, domain),
    )
    await cur.execute(
        f"SELECT domain_revision,state_fingerprint FROM {online}`_task_view_state_revisions` "
        "WHERE source_id=%s AND domain=%s FOR UPDATE",
        (source_id, domain),
    )
    previous = await cur.fetchone()
    task_revision = int(source[2])
    fingerprint = hmac.new(
        _key(),
        canonical_json({
            "domain": domain, "task_revision": task_revision,
            "removed": removed, "state": state,
        }),
        hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(str(previous[1] or ""), fingerprint):
        return None
    revision = int(previous[0]) + 1
    await cur.execute(
        f"UPDATE {online}`_task_view_state_revisions` "
        "SET domain_revision=%s,task_revision=%s,state_fingerprint=%s "
        "WHERE source_id=%s AND domain=%s",
        (revision, task_revision, fingerprint, source_id, domain),
    )
    return await enqueue_task_view_state_event(
        cur, environment=settings.APP_ENVIRONMENT,
        run_id=settings.TASK_READ_MODEL_RUN_ID, task_id=task_id,
        source_id=source_id, domain=domain, domain_revision=revision,
        task_revision=task_revision, state=state, removed=removed,
        operation_id=operation_id,
        outbox_table=f"{online_db}._domain_event_outbox",
    )


async def enqueue_online_task_graph_view_state(
    cur, *, parser_type: str, row_key: str,
) -> None:
    """Project the current safe dependency state for every active source row."""
    from config import settings

    if not task_view_state_events_enabled():
        return
    if not isinstance(parser_type, str) or not isinstance(row_key, str) or not row_key:
        raise TaskCommandContractError("invalid task graph view state identity")
    workflow_db = str(settings.MYSQL_WORKFLOW_DB or "")
    online_db = str(settings.MYSQL_ONLINE_DATA_DB or "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", workflow_db) or not re.fullmatch(r"[A-Za-z0-9_]+", online_db):
        raise TaskCommandContractError("task graph view state database identity invalid")
    workflow = f"`{workflow_db}`."
    online = f"`{online_db}`."
    await cur.execute(
        f"SELECT online_node.status,analysis.status,dependency.state "
        f"FROM {workflow}`task_graph_nodes` online_node "
        f"JOIN {workflow}`task_graph_dependencies` dependency "
        "ON dependency.successor_node_id=online_node.id "
        f"JOIN {workflow}`task_graph_nodes` analysis "
        "ON analysis.id=dependency.predecessor_node_id "
        "WHERE online_node.task_type='online_check' AND online_node.provider='online' "
        "AND online_node.parser_type=%s AND online_node.source_ref=%s "
        "AND analysis.task_type='analysis' AND analysis.provider='online' "
        "AND analysis.parser_type=online_node.parser_type "
        "AND analysis.source_ref=online_node.source_ref "
        "AND dependency.reason_code='analysis_before_followup' "
        "ORDER BY dependency.id DESC LIMIT 1",
        (parser_type, row_key),
    )
    row = await cur.fetchone()
    if row:
        online_status, analysis_status, dependency_state = map(lambda value: str(value or ""), row)
        state = {
            "state": "blocked" if dependency_state == "active" else "ready",
            "blocked": dependency_state == "active",
            "online_status": online_status,
            "analysis_status": analysis_status,
            "dependency_state": dependency_state,
        }
    else:
        state = {
            "state": "not_applicable", "blocked": False,
            "online_status": "", "analysis_status": "",
            "dependency_state": "",
        }
    await cur.execute(
        f"SELECT id FROM {online}`_online_source_rows` "
        "WHERE parser_type=%s AND row_key=%s AND archived_at IS NULL",
        (parser_type, row_key),
    )
    for (source_id,) in await cur.fetchall():
        await enqueue_source_task_view_state(
            cur, source_id=int(source_id), domain="task_graph", state=state,
        )


async def enqueue_current_task_view_state_snapshot(
    cur, *, source_id: int, snapshot: dict[str, Any], removed: bool = False,
) -> None:
    """Append all current domain states alongside a task command event."""
    from config import settings
    from services.parsers import get_parser
    from services.task_workflow import TASK_WORKFLOWS
    from services.task_registration import is_pending_registration, is_registration_task
    from services.unverifiable_review import is_unverifiable_result, supports_unverifiable_review

    if not task_view_state_events_enabled():
        return
    online_db = str(settings.MYSQL_ONLINE_DATA_DB or "")
    registry_db = str(settings.MYSQL_REGISTRY_DB or "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", online_db) or not re.fullmatch(r"[A-Za-z0-9_]+", registry_db):
        raise TaskCommandContractError("task view state database identity invalid")
    online = f"`{online_db}`."
    registry = f"`{registry_db}`."
    await cur.execute(
        f"SELECT parser_type,physical_row,revision,row_hash,archived_at,row_key "
        f"FROM {online}`_online_source_rows` WHERE id=%s",
        (source_id,),
    )
    source = await cur.fetchone()
    if not source:
        raise TaskCommandContractError("task view state source unavailable")
    parser_type, physical_row, revision = str(source[0]), int(source[1]), int(source[2])
    row_key = str(source[5] or "")
    task_id = f"{get_parser(parser_type).table_name}:{physical_row}"
    if removed:
        for domain in TASK_VIEW_STATE_DOMAINS:
            await enqueue_source_task_view_state(
                cur, source_id=source_id, domain=domain, state={},
                removed=True, include_archived=True,
            )
        return

    states: dict[str, dict[str, Any]] = {
        "address_match": {"status": "unmatched", "candidate": None, "candidates": []},
        "watch": {"status": "none", "first_dispatch_at": "", "watch_marks": []},
        "qmf": {"status": "not_applicable"},
        "residence": {"status": "not_applicable"},
        "registration": {"status": "not_applicable"},
        "review": {"state": "not_applicable"},
        "task_graph": {
            "state": "not_applicable", "blocked": False,
            "online_status": "", "analysis_status": "",
            "dependency_state": "",
        },
    }
    await cur.execute(
        "SELECT suggested_entry_id,suggested_community_id,suggested_community_name,"
        "match_status,match_score,match_method,match_reason,candidates_json,matcher_version,"
        "confirmed_entry_id,manual_unmatched_reason FROM " + online + "`_online_task_address_matches` "
        "WHERE parser_type=%s AND row_key=%s",
        (parser_type, row_key),
    )
    row = await cur.fetchone()
    if row:
        raw_candidates = row[7]
        if isinstance(raw_candidates, (str, bytes)):
            raw_candidates = json.loads(raw_candidates or "[]")
        candidates = raw_candidates if isinstance(raw_candidates, list) else []
        selected_id = int(row[9] or row[0] or 0) or None
        candidate = next((
            item for item in candidates
            if isinstance(item, dict) and int(item.get("entry_id") or 0) == selected_id
        ), None)
        if candidate is None and selected_id:
            candidate = {
                "entry_id": selected_id,
                "community_id": int(row[1]) if row[1] is not None else None,
                "community_name": str(row[2] or ""),
            }
        states["address_match"] = {
            "status": str(row[3] or "unmatched"), "score": float(row[4] or 0),
            "method": str(row[5] or ""), "reason": str(row[6] or ""),
            "candidate": candidate, "candidates": candidates,
            "version": str(row[8] or ""),
            "manual_unmatched_reason": str(row[10] or ""),
        }

    await cur.execute(
        f"SELECT category.id,category.name,category.color,category.alert_level,"
        f"assignment.status,assignment.source_type,snapshot.snapshot_status,snapshot.snapshot_reason,"
        f"snapshot.first_dispatch_at FROM {registry}`online_task_watch_snapshots` snapshot "
        f"JOIN {registry}`watch_assignments` assignment ON assignment.id=snapshot.assignment_id "
        f"JOIN {registry}`watch_categories` category ON category.id=assignment.category_id "
        "WHERE snapshot.parser_type=%s AND snapshot.row_key=%s ORDER BY category.sort_order,category.id",
        (parser_type, row_key),
    )
    marks = []
    first_dispatch = ""
    for watch in await cur.fetchall():
        first_dispatch = first_dispatch or str(watch[8] or "")
        marks.append({
            "category_id": int(watch[0]), "name": str(watch[1] or ""),
            "color": str(watch[2] or ""), "alert_level": str(watch[3] or ""),
            "assignment_status": str(watch[4] or ""), "source_type": str(watch[5] or ""),
            "snapshot_status": str(watch[6] or ""), "snapshot_reason": str(watch[7] or ""),
        })
    states["watch"] = {
        "status": "active" if marks else "none",
        "first_dispatch_at": first_dispatch, "watch_marks": marks,
    }

    if parser_type == "疑似未注销模型三":
        await cur.execute(
            f"SELECT feedback_state,platform_result,feedback_result,checked_at,origin,error_code,"
            f"last_scanned_at,source_revision,source_row_hash "
            f"FROM {online}`_qmf_status_snapshots` WHERE parser_type=%s AND row_key=%s",
            (parser_type, row_key),
        )
        row = await cur.fetchone()
        qmf_state = str(row[0] or "not_scanned") if row else "not_scanned"
        if row:
            from datetime import datetime, timedelta
            from services.qmf_status import normalize_qmf_status_result

            last_scanned = row[6]
            stale = (
                normalize_qmf_status_result(row[1])
                != normalize_qmf_status_result(snapshot.get("核查结果"))
                or int(row[7] or 0) != revision
                or str(row[8] or "") != str(source[3] or "")
                or not last_scanned
                or (datetime.utcnow() - last_scanned).total_seconds() > 7 * 24 * 60 * 60
            )
            if row[5]:
                qmf_state = "error"
            elif stale:
                qmf_state = "stale"
        states["qmf"] = ({
            "state": qmf_state, "status": qmf_state,
            "platform_result": str(row[1] or "") if row else "",
            "result": str(row[2] or ""), "checked_at": str(row[3] or ""),
            "origin": str(row[4] or "") if row else "",
            "error_code": str(row[5] or "") if row else "",
            "last_scanned_at": str(row[6] or "") if row else "",
        })

    if parser_type in TASK_WORKFLOWS:
        await cur.execute(
            f"SELECT status,error_code,checked_at,last_attempt_at,duration_ms "
            f"FROM {online}`_residence_registration_status` WHERE parser_type=%s AND row_key=%s",
            (parser_type, row_key),
        )
        row = await cur.fetchone()
        states["residence"] = ({
            "state": str(row[0] or "pending"), "status": str(row[0] or "pending"),
            "error_code": str(row[1] or ""),
            "checked_at": str(row[2] or ""), "last_attempt_at": str(row[3] or ""),
            "duration_ms": int(row[4]) if row[4] is not None else None,
        } if row else {"state": "pending", "status": "pending"})

        await cur.execute(
            f"SELECT property_id,property_version,status,reason_code,match_count,selected_by,confirmed_by,manual_reason "
            f"FROM {online}`_task_registration_links` WHERE parser_type=%s AND row_key=%s",
            (parser_type, row_key),
        )
        row = await cur.fetchone()
        if row:
            states["registration"] = {
                "property_id": int(row[0]) if row[0] is not None else None,
                "property_version": int(row[1]) if row[1] is not None else None,
                "status": str(row[2] or ""), "reason_code": str(row[3] or ""),
                "match_count": int(row[4] or 0),
                "selected_by": int(row[5]) if row[5] is not None else None,
                "confirmed_by": int(row[6]) if row[6] is not None else None,
                "manual_reason": str(row[7] or ""),
            }
        elif is_registration_task(parser_type) and is_pending_registration(parser_type, snapshot):
            states["registration"] = {"status": "awaiting_match", "reason_code": ""}

    if supports_unverifiable_review(parser_type):
        await cur.execute(
            f"SELECT id,state,flow_version,review_due_date,feedback_submitted,safe_reason_code "
            f"FROM {online}`_unverifiable_review_flows` WHERE parser_type=%s AND row_key=%s "
            "ORDER BY cycle_no DESC LIMIT 1",
            (parser_type, row_key),
        )
        row = await cur.fetchone()
        if row:
            due = row[3]
            states["review"] = {
                "flow_id": int(row[0]), "state": str(row[1] or ""),
                "flow_version": int(row[2] or 0),
                "review_due_date": due.isoformat() if hasattr(due, "isoformat") else str(due or ""),
                "feedback_submitted": bool(row[4]), "safe_reason_code": str(row[5] or ""),
            }
        elif is_unverifiable_result(
            snapshot.get(TASK_WORKFLOWS[parser_type].result_field)
        ):
            raise TaskCommandContractError("task review flow is not initialized")

    for domain, state in states.items():
        await enqueue_source_task_view_state(
            cur, source_id=source_id, domain=domain, state=state,
        )
    await enqueue_online_task_graph_view_state(
        cur, parser_type=parser_type, row_key=row_key,
    )


def _key() -> bytes:
    from config import settings

    secret = str(settings.TASK_EVENT_ENCRYPTION_KEY or "")
    if len(secret.encode("utf-8")) < 32:
        raise TaskCommandContractError("task view state encryption key unavailable")
    return hashlib.sha256(
        b"binhu/task-view-state/aes-gcm/v1\0" + secret.encode("utf-8")
    ).digest()


def _aad(*, environment: str, run_id: str, task_id: str, domain: str,
         domain_revision: int, task_revision: int, event_id: str) -> bytes:
    return canonical_json({
        "contract": "binhu.task-view-state.v1",
        "domain": domain,
        "domain_revision": domain_revision,
        "environment": environment,
        "event_id": event_id,
        "run_id": run_id,
        "task_id": task_id,
        "task_revision": task_revision,
    })


def build_task_view_state_update(
    *, environment: str, run_id: str, task_id: str, domain: str,
    domain_revision: int, task_revision: int, event_id: str,
    observed_at: str, state: dict[str, Any], removed: bool = False,
) -> dict[str, Any]:
    """Validate and encrypt one domain-owned task state update."""
    keys = task_view_state_keys(environment, run_id, task_id, domain)
    if (type(domain_revision) is not int or domain_revision < 1
            or type(task_revision) is not int or task_revision < 1):
        raise TaskCommandContractError("invalid task view state revision")
    try:
        event_id = str(uuid.UUID(event_id))
    except (ValueError, TypeError, AttributeError):
        raise TaskCommandContractError("invalid task view state event id") from None
    if (not isinstance(observed_at, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", observed_at)):
        raise TaskCommandContractError("invalid task view state timestamp")
    if type(removed) is not bool or not isinstance(state, dict):
        raise TaskCommandContractError("invalid task view state payload")
    if removed and state:
        raise TaskCommandContractError("removed task view state must be empty")
    if not removed and not state:
        raise TaskCommandContractError("task view state payload is required")
    try:
        plaintext = canonical_json(state)
        if len(plaintext) > 64 * 1024:
            raise TaskCommandContractError("task view state payload too large")
        nonce = os.urandom(12)
        aad = _aad(
            environment=environment, run_id=run_id, task_id=task_id,
            domain=domain, domain_revision=domain_revision,
            task_revision=task_revision, event_id=event_id,
        )
        ciphertext = AESGCM(_key()).encrypt(nonce, plaintext, aad)
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task view state encryption failed") from None
    envelope = {
        "algorithm": "AES-256-GCM",
        "key_version": "task-view-state-v1",
        "nonce": base64.urlsafe_b64encode(nonce).decode().rstrip("="),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode().rstrip("="),
        "aad": base64.urlsafe_b64encode(aad).decode().rstrip("="),
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
    }
    return {
        "keys": keys[:2],
        "index_keys": task_view_state_index_keys(
            environment=environment, run_id=run_id, domain=domain,
            state=state, removed=removed,
        ),
        "environment": environment,
        "run_id": run_id,
        "task_id": task_id,
        "domain": domain,
        "domain_revision": domain_revision,
        "task_revision": task_revision,
        "event_id": event_id,
        "observed_at": observed_at,
        "removed": removed,
        "envelope": envelope,
    }


def decrypt_task_view_state(update: dict[str, Any]) -> dict[str, Any]:
    """Authenticate a state envelope against its complete domain identity."""
    envelope = update.get("envelope")
    if not isinstance(envelope, dict) or envelope.get("algorithm") != "AES-256-GCM" \
            or envelope.get("key_version") != "task-view-state-v1":
        raise TaskCommandContractError("invalid task view state envelope")
    try:
        aad = base64.urlsafe_b64decode(envelope["aad"] + "=" * (-len(envelope["aad"]) % 4))
        nonce = base64.urlsafe_b64decode(envelope["nonce"] + "=" * (-len(envelope["nonce"]) % 4))
        ciphertext = base64.urlsafe_b64decode(
            envelope["ciphertext"] + "=" * (-len(envelope["ciphertext"]) % 4)
        )
        if hashlib.sha256(ciphertext).hexdigest() != envelope.get("ciphertext_sha256"):
            raise ValueError
        expected_aad = _aad(
            environment=update["environment"], run_id=update["run_id"],
            task_id=update["task_id"], domain=update["domain"],
            domain_revision=update["domain_revision"],
            task_revision=update["task_revision"], event_id=update["event_id"],
        )
        if aad != expected_aad:
            raise ValueError
        plaintext = AESGCM(_key()).decrypt(nonce, ciphertext, aad)
        state = json.loads(plaintext)
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task view state authentication failed") from None
    if not isinstance(state, dict):
        raise TaskCommandContractError("invalid task view state payload")
    return state


_APPLY_TASK_VIEW_STATE = """
local current = tonumber(redis.call('HGET', KEYS[1], 'domain_revision') or '-1')
local incoming = tonumber(ARGV[1])
if redis.call('HGET', KEYS[1], 'event_id') == ARGV[2] then return 'duplicate' end
if incoming < current then return 'stale' end
if incoming == current and current >= 0 then return 'revision_conflict' end
local members_key = ARGV[15]
for _, index_key in ipairs(redis.call('SMEMBERS', members_key)) do
  redis.call('SREM', index_key, ARGV[5])
  if redis.call('SCARD', index_key) == 0 then redis.call('DEL', index_key) end
end
redis.call('DEL', members_key)
redis.call('HSET', KEYS[1], 'environment', ARGV[3], 'run_id', ARGV[4],
  'task_id', ARGV[5], 'domain', ARGV[6], 'domain_revision', ARGV[1],
  'task_revision', ARGV[7], 'event_id', ARGV[2], 'observed_at', ARGV[8],
  'removed', ARGV[9], 'ciphertext', ARGV[10], 'aad', ARGV[11],
  'nonce', ARGV[12], 'ciphertext_sha256', ARGV[13], 'key_version', ARGV[14])
local index_count = tonumber(ARGV[16])
for i = 1, index_count do
  local index_key = ARGV[16 + i]
  redis.call('SADD', index_key, ARGV[5])
  redis.call('SADD', members_key, index_key)
end
return 'applied'
"""


async def apply_task_view_state_update(redis_client, update: dict[str, Any]) -> str:
    if not isinstance(update, dict) or type(update.get("removed")) is not bool:
        raise TaskCommandContractError("invalid task view state update")
    expected_keys = task_view_state_keys(
        update["environment"], update["run_id"], update["task_id"], update["domain"],
    )
    if tuple(update.get("keys") or ()) != expected_keys[:2]:
        raise TaskCommandContractError("invalid task view state Redis key")
    state = decrypt_task_view_state(update)
    expected_indexes = task_view_state_index_keys(
        environment=update["environment"], run_id=update["run_id"],
        domain=update["domain"], state=state, removed=update["removed"],
    )
    if tuple(update.get("index_keys") or ()) != expected_indexes:
        raise TaskCommandContractError("invalid task view state index keys")
    envelope = update["envelope"]
    try:
        return await redis_client.eval(
            _APPLY_TASK_VIEW_STATE, 1, expected_keys[0],
            update["domain_revision"], update["event_id"], update["environment"],
            update["run_id"], update["task_id"], update["domain"],
            update["task_revision"], update["observed_at"],
            "1" if update["removed"] else "0", envelope["ciphertext"],
            envelope["aad"], envelope["nonce"], envelope["ciphertext_sha256"],
            envelope["key_version"], expected_keys[1], len(expected_indexes),
            *expected_indexes,
        )
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task view state Redis update failed") from None


async def read_task_view_state(
    redis_client, *, environment: str, run_id: str, task_id: str,
    domain: str, expected_task_revision: int | None = None,
) -> dict[str, Any] | None:
    """Read one state only when its Redis identity and encrypted contract match."""
    record_key, status_key, _prefix = task_view_state_keys(
        environment, run_id, task_id, domain,
    )
    try:
        record = await redis_client.hmget(
            record_key,
            "environment", "run_id", "task_id", "domain", "domain_revision",
            "task_revision", "event_id", "observed_at", "removed", "ciphertext",
            "aad", "nonce", "ciphertext_sha256", "key_version",
        )
    except Exception:
        raise TaskCommandContractError("task view state Redis read failed") from None
    if not record or all(value is None for value in record):
        return None
    (stored_environment, stored_run_id, stored_task_id, stored_domain,
     raw_domain_revision, raw_task_revision, event_id, observed_at, raw_removed,
     ciphertext, aad, nonce, digest, key_version) = record
    try:
        domain_revision = int(raw_domain_revision)
        task_revision = int(raw_task_revision)
        removed = raw_removed == "1"
        if raw_removed not in {"0", "1"}:
            raise ValueError
        if (stored_environment != environment or stored_run_id != run_id
                or stored_task_id != task_id or stored_domain != domain
                or domain_revision < 1 or task_revision < 1
                or (expected_task_revision is not None
                    and (type(expected_task_revision) is not int
                         or expected_task_revision != task_revision))):
            raise ValueError
        update = {
            "environment": environment, "run_id": run_id, "task_id": task_id,
            "domain": domain, "domain_revision": domain_revision,
            "task_revision": task_revision, "event_id": event_id,
            "observed_at": observed_at, "removed": removed,
            "keys": (record_key, status_key),
            "envelope": {
                "algorithm": "AES-256-GCM", "key_version": key_version,
                "ciphertext": ciphertext, "aad": aad, "nonce": nonce,
                "ciphertext_sha256": digest,
            },
        }
        state = decrypt_task_view_state(update)
        index_keys = task_view_state_index_keys(
            environment=environment, run_id=run_id, domain=domain,
            state=state, removed=removed,
        )
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task view state identity mismatch") from None
    if removed and state:
        raise TaskCommandContractError("removed task view state is not empty")
    if not removed and not state:
        raise TaskCommandContractError("task view state payload is empty")
    return {
        "domain": domain,
        "domain_revision": domain_revision,
        "task_revision": task_revision,
        "event_id": event_id,
        "observed_at": observed_at,
        "removed": removed,
        "state": state,
        "index_keys": index_keys,
    }


async def read_task_view_states_for_tasks(
    redis_client, *, environment: str, run_id: str,
    tasks: list[dict[str, Any]],
    required_domains: frozenset[str] = TASK_VIEW_STATE_DOMAINS,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Read complete, revision-compatible state for each task in one request."""
    if not isinstance(tasks, list) or not required_domains.issubset(TASK_VIEW_STATE_DOMAINS):
        raise TaskCommandContractError("invalid task view state query")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    seen: set[str] = set()
    requests: list[tuple[str, str, int, tuple[str, str, str]]] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise TaskCommandContractError("invalid task view state query")
        task_id = task.get("task_id")
        revision = task.get("revision")
        if (not isinstance(task_id, str) or type(revision) is not int or revision < 1
                or task_id in seen):
            raise TaskCommandContractError("invalid task view state query")
        seen.add(task_id)
        result[task_id] = {}
        for domain in TASK_VIEW_STATE_DOMAINS:
            requests.append((task_id, domain, revision,
                             task_view_state_keys(environment, run_id, task_id, domain)))
    if not requests:
        return result
    fields = (
        "environment", "run_id", "task_id", "domain", "domain_revision",
        "task_revision", "event_id", "observed_at", "removed", "ciphertext",
        "aad", "nonce", "ciphertext_sha256", "key_version",
    )
    class _BatchReader:
        def __init__(self, batch_requests, values):
            self.requests = batch_requests
            self.values = values
            self.index = 0

        async def hmget(self, key, *_fields):
            request = self.requests[self.index]
            if key != request[3][0] or tuple(_fields) != fields:
                raise TaskCommandContractError("task view state batch identity mismatch")
            self.index += 1
            return self.values[self.index - 1]

    for start in range(0, len(requests), 300):
        batch_requests = requests[start:start + 300]
        try:
            pipeline = redis_client.pipeline(transaction=False)
            for _task_id, _domain, _revision, keys in batch_requests:
                pipeline.hmget(keys[0], *fields)
            rows = await pipeline.execute()
        except Exception:
            raise TaskCommandContractError("task view state Redis read failed") from None
        if not isinstance(rows, (list, tuple)) or len(rows) != len(batch_requests):
            raise TaskCommandContractError("task view state Redis read failed")
        batch_reader = _BatchReader(batch_requests, rows)
        for task_id, domain, revision, _keys in batch_requests:
            state = await read_task_view_state(
                batch_reader, environment=environment, run_id=run_id,
                task_id=task_id, domain=domain, expected_task_revision=revision,
            )
            if state is None:
                if domain in required_domains:
                    raise TaskCommandContractError("task view state is not initialized")
                continue
            result[task_id][domain] = state
    return result


__all__ = [
    "TASK_VIEW_STATE_DOMAINS", "TASK_VIEW_STATE_INDEX_VERSION",
    "TASK_VIEW_STATE_REVISIONS_SQL",
    "apply_task_view_state_update", "enqueue_source_task_view_state",
    "enqueue_online_task_graph_view_state",
    "build_task_view_state_update", "decrypt_task_view_state",
    "read_task_view_state", "read_task_view_states_for_tasks",
    "task_view_state_events_enabled",
    "task_view_state_keys",
    "task_view_state_index_keys",
]
