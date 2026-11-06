"""Strict, non-sensitive contract for the online-summary derived view.

Task snapshots are not summary facts.  A producer must explicitly provide
the business day and first-ownership tokens before a record can be reduced.
This keeps the Flink projection from guessing history from a current task
snapshot and makes the view rebuildable from an immutable event stream.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping


CONTRACT_VERSION = 1
NAMESPACE = "binhu:online-summary:v1"
SUMMARY_EVENT_TYPES = frozenset({"summary.task.changed", "summary.task.deleted"})
TASK_STATES = frozenset({"unchecked", "checked", "completed"})
ACTIVITY_KINDS = frozenset({"carryover", "new", "changed", "state_transition", "removed"})
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_RE = re.compile(r"^(?:dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}|STG-[0-9]{8}-[0-9]{2})$")
TASK_RE = re.compile(r"^t_[a-z0-9_]+:[1-9][0-9]{0,18}$")
PARSER_RE = re.compile(r"^[^\r\n]{1,80}$")


class OnlineSummaryContractError(ValueError):
    """Raised when a summary fact cannot be safely reduced."""


def _required_text(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise OnlineSummaryContractError(f"invalid online summary field: {field}")
    return value


def _int(value: Any, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        raise OnlineSummaryContractError(f"invalid online summary field: {field}")
    return value


def validate_summary_fact(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize one encrypted-payload-derived summary fact.

    Tokens are HMAC outputs and must never be replaced by display names.  The
    returned object contains only bounded aggregation fields and may be sent
    to a reducer or a Redis sink.
    """
    if not isinstance(value, Mapping):
        raise OnlineSummaryContractError("invalid online summary fact")
    allowed = {
        "contract_version", "event_type", "event_id", "command_id", "operation_id",
        "environment", "run_id", "task_id", "source_id", "revision", "task_revision",
        "business_date", "parser_type", "row_key_hmac", "community_token", "inspector_token",
        "task_state", "included", "online_present", "unable_to_verify", "reached_bottom",
        "effective_workload", "activity_kind", "correction", "deleted", "occurred_at",
    }
    if any(key not in allowed for key in value):
        raise OnlineSummaryContractError("invalid online summary properties")
    if value.get("contract_version") != CONTRACT_VERSION:
        raise OnlineSummaryContractError("invalid online summary contract_version")
    event_type = _required_text(value.get("event_type"), "event_type", re.compile(r"^[a-z.]{1,64}$"))
    if event_type not in SUMMARY_EVENT_TYPES:
        raise OnlineSummaryContractError("invalid online summary event_type")
    for field in ("event_id", "command_id", "operation_id"):
        _required_text(value.get(field), field, re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        ))
    environment = _required_text(value.get("environment"), "environment", re.compile(r"^(development|staging)$"))
    run_id = _required_text(value.get("run_id"), "run_id", RUN_RE)
    if (environment == "development" and not run_id.startswith("dev-")) or (
        environment == "staging" and not run_id.startswith("STG-")
    ):
        raise OnlineSummaryContractError("online summary environment/run mismatch")
    task_id = _required_text(value.get("task_id"), "task_id", TASK_RE)
    source_id = _int(value.get("source_id"), "source_id", 1)
    revision = _int(value.get("revision"), "revision", 1)
    task_revision = _int(value.get("task_revision"), "task_revision", 1)
    try:
        business_date = date.fromisoformat(str(value.get("business_date")))
    except (TypeError, ValueError):
        raise OnlineSummaryContractError("invalid online summary field: business_date") from None
    parser_type = _required_text(value.get("parser_type"), "parser_type", PARSER_RE)
    row_key_hmac = _required_text(value.get("row_key_hmac"), "row_key_hmac", TOKEN_RE)
    community_token = _required_text(value.get("community_token"), "community_token", TOKEN_RE)
    inspector_token = _required_text(value.get("inspector_token"), "inspector_token", TOKEN_RE)
    task_state = value.get("task_state")
    if task_state not in TASK_STATES:
        raise OnlineSummaryContractError("invalid online summary field: task_state")
    activity_kind = value.get("activity_kind")
    if activity_kind not in ACTIVITY_KINDS:
        raise OnlineSummaryContractError("invalid online summary field: activity_kind")
    correction = value.get("correction")
    deleted = value.get("deleted")
    if type(correction) is not bool or type(deleted) is not bool:
        raise OnlineSummaryContractError("invalid online summary correction/deleted flags")
    occurred_at = _required_text(
        value.get("occurred_at"), "occurred_at",
        re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"),
    )
    try:
        from datetime import datetime
        datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError:
        raise OnlineSummaryContractError("invalid online summary field: occurred_at") from None
    if deleted != (event_type == "summary.task.deleted"):
        raise OnlineSummaryContractError("online summary deletion identity mismatch")
    if deleted != (activity_kind == "removed"):
        raise OnlineSummaryContractError("online summary removal activity mismatch")
    result = {
        "contract_version": CONTRACT_VERSION,
        "event_type": event_type,
        "event_id": value["event_id"],
        "command_id": value["command_id"],
        "operation_id": value["operation_id"],
        "environment": environment,
        "run_id": run_id,
        "task_id": task_id,
        "source_id": source_id,
        "revision": revision,
        "task_revision": task_revision,
        "business_date": business_date.isoformat(),
        "parser_type": parser_type,
        "row_key_hmac": row_key_hmac,
        "community_token": community_token,
        "inspector_token": inspector_token,
        "task_state": task_state,
        "included": _int(value.get("included"), "included"),
        "online_present": _int(value.get("online_present"), "online_present"),
        "unable_to_verify": _int(value.get("unable_to_verify"), "unable_to_verify"),
        "reached_bottom": _int(value.get("reached_bottom"), "reached_bottom"),
        "effective_workload": _int(value.get("effective_workload"), "effective_workload"),
        "activity_kind": activity_kind,
        "correction": correction,
        "deleted": deleted,
        "occurred_at": occurred_at,
    }
    if result["included"] > 1 or result["online_present"] > 1:
        raise OnlineSummaryContractError("online summary boolean flag out of range")
    return result


def summary_namespace(environment: str, run_id: str) -> str:
    """Return an isolated Redis namespace after validating run identity."""
    if environment not in {"development", "staging"} or RUN_RE.fullmatch(run_id) is None:
        raise OnlineSummaryContractError("invalid online summary namespace identity")
    if (environment == "development") != run_id.startswith("dev-"):
        raise OnlineSummaryContractError("online summary namespace environment mismatch")
    if (environment == "staging") != run_id.startswith("STG-"):
        raise OnlineSummaryContractError("online summary namespace environment mismatch")
    return f"{NAMESPACE}:{environment}:{run_id}"


def watermark_record(*, environment: str, run_id: str, generation: int,
                     complete_through_business_date: str | None,
                     kafka_offsets: Mapping[str, int], computed_at: str) -> dict[str, Any]:
    """Build the bounded readiness watermark stored beside the view."""
    namespace = summary_namespace(environment, run_id)
    generation = _int(generation, "generation", 1)
    if complete_through_business_date is not None:
        try:
            date.fromisoformat(complete_through_business_date)
        except ValueError:
            raise OnlineSummaryContractError("invalid watermark business date") from None
    if not isinstance(kafka_offsets, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, int) or value < 0
        for key, value in kafka_offsets.items()
    ):
        raise OnlineSummaryContractError("invalid watermark offsets")
    return {
        "contract_version": CONTRACT_VERSION,
        "namespace": namespace,
        "environment": environment,
        "run_id": run_id,
        "generation": generation,
        "complete_through_business_date": complete_through_business_date,
        "kafka_offsets": dict(sorted(kafka_offsets.items())),
        "computed_at": _required_text(computed_at, "computed_at", re.compile(r"^[^\r\n]{1,64}$")),
    }


__all__ = [
    "CONTRACT_VERSION", "NAMESPACE", "SUMMARY_EVENT_TYPES", "ACTIVITY_KINDS", "OnlineSummaryContractError",
    "summary_namespace", "validate_summary_fact", "watermark_record",
]
