"""Contract helpers for the platform online-summary Redis read model.

The online summary is a derived view of daily task facts.  It is deliberately
separate from the task read model: a latest task snapshot cannot reproduce
carryover, changed/new categories or effective workload history.  This module
contains only the shared identity, key and fail-closed decoding contract.  A
caller must not fall back to the legacy daily-report tables after a read-model
read has started.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Any, Mapping


SUMMARY_NAMESPACE = "binhu:online-summary:v1"
SUMMARY_STATE_READY = "ready"
SUMMARY_STATE_REBUILDING = "rebuilding"
SUMMARY_STATES = frozenset({SUMMARY_STATE_READY, SUMMARY_STATE_REBUILDING})
SUMMARY_PARSER_RE = re.compile(r"^[^:/\\\x00-\x1f]{1,80}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TASK_RE = re.compile(r"^t_[a-z0-9_]+:[1-9][0-9]{0,18}$")
SUMMARY_STATES = frozenset({"unchecked", "checked", "completed"})
SUMMARY_ACTIVITY_KINDS = frozenset({"carryover", "new", "changed", "state_transition", "removed"})
PARSER_TYPE_RE = re.compile(r"^[^:/\\\x00-\x1f]{1,80}$")


class OnlineSummaryReadModelError(RuntimeError):
    """A safe, fixed-category read-model failure."""


class OnlineSummaryContractError(ValueError):
    """Invalid identity or aggregate data supplied by a caller/sink."""


def summary_fact_rebuild_reason(
    *, current_present: bool, has_same_day_previous: bool,
    has_historical_previous: bool, revision_gap: bool,
) -> str | None:
    """Refuse facts whose daily activity cannot be classified from the ledger."""
    if revision_gap:
        return "revision_gap"
    if current_present and not has_same_day_previous and has_historical_previous:
        return "cross_day_activity_requires_snapshot"
    return None


@dataclass(frozen=True)
class SummaryIdentity:
    environment: str
    run_id: str

    def __post_init__(self) -> None:
        if self.environment not in {"development", "staging", "production", "shadow"}:
            raise OnlineSummaryContractError("invalid online summary environment")
        if not RUN_RE.fullmatch(self.run_id):
            raise OnlineSummaryContractError("invalid online summary run_id")

    @property
    def prefix(self) -> str:
        return f"{SUMMARY_NAMESPACE}:{self.environment}:{self.run_id}"


@dataclass(frozen=True)
class SummaryAggregate:
    """A privacy-safe aggregate row stored in Redis.

    ``community_token`` and ``inspector_token`` are HMACs.  Display labels are
    resolved by the Backend against the authoritative directory after scope
    checks; they are never stored in this view.
    """

    business_date: str
    parser_type: str
    community_token: str
    inspector_token: str
    data_total: int
    unchecked: int
    checked: int
    completed: int
    unable_to_verify: int
    effective_workload: int
    revision: int

    def __post_init__(self) -> None:
        if not DATE_RE.fullmatch(self.business_date):
            raise OnlineSummaryContractError("invalid online summary business_date")
        try:
            date.fromisoformat(self.business_date)
        except ValueError as exc:
            raise OnlineSummaryContractError("invalid online summary business_date") from exc
        if not SUMMARY_PARSER_RE.fullmatch(self.parser_type):
            raise OnlineSummaryContractError("invalid online summary parser_type")
        for name in ("community_token", "inspector_token"):
            if not TOKEN_RE.fullmatch(getattr(self, name)):
                raise OnlineSummaryContractError(f"invalid online summary {name}")
        for name in (
            "data_total", "unchecked", "checked", "completed",
            "unable_to_verify", "effective_workload", "revision",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise OnlineSummaryContractError(f"invalid online summary {name}")
        if self.revision < 1:
            raise OnlineSummaryContractError("invalid online summary revision")
        if self.unchecked + self.checked + self.completed > self.data_total:
            raise OnlineSummaryContractError("online summary status counts exceed total")

    @property
    def key_suffix(self) -> str:
        return (
            f"aggregate:{self.business_date}:{self.parser_type}:"
            f"{self.community_token}:{self.inspector_token}"
        )

    def redis_mapping(self) -> dict[str, str]:
        return {
            "business_date": self.business_date,
            "parser_type": self.parser_type,
            "community_token": self.community_token,
            "inspector_token": self.inspector_token,
            "data_total": str(self.data_total),
            "unchecked": str(self.unchecked),
            "checked": str(self.checked),
            "completed": str(self.completed),
            "unable_to_verify": str(self.unable_to_verify),
            "effective_workload": str(self.effective_workload),
            "revision": str(self.revision),
        }


def summary_dimension_token(
    value: Any, *, dimension: str, environment: str, secret: str,
) -> str:
    """Return an environment-scoped HMAC token for a directory dimension."""
    if dimension not in {"community", "inspector", "task_row"}:
        raise OnlineSummaryContractError("unsupported online summary dimension")
    if not secret or len(secret.encode("utf-8")) < 32:
        raise OnlineSummaryContractError("dedicated online summary key is required")
    text = str(value or "").strip()
    if dimension != "task_row":
        text = text.lower()
    if not text or len(text) > 512 or any(ord(char) < 32 for char in text):
        raise OnlineSummaryContractError("invalid online summary dimension value")
    if environment not in {"development", "staging", "production", "shadow"}:
        raise OnlineSummaryContractError("invalid online summary environment")
    message = f"binhu/online-summary/v1\0{environment}\0{dimension}\0{text}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_online_summary_fact(
    *,
    event_id: str,
    command_id: str,
    operation_id: str,
    environment: str,
    run_id: str,
    task_id: str,
    row_key: str,
    source_id: int,
    revision: int,
    task_revision: int,
    business_date: str,
    parser_type: str,
    first_community: str,
    first_inspector: str,
    task_state: str,
    included: int,
    online_present: int,
    unable_to_verify: int,
    reached_bottom: int,
    effective_workload: int,
    activity_kind: str,
    occurred_at: str,
    secret: str,
    deleted: bool = False,
    correction: bool = False,
) -> dict[str, Any]:
    """Build one privacy-safe, replayable fact from authoritative values.

    This function deliberately requires first ownership and business-day
    semantics from the caller. It never infers them from a current snapshot.
    ``effective_workload`` must come from the legacy ledger transition
    calculation; this function only validates its per-task/day bound.
    Missing ownership is a data-quality/rebuild condition, not an empty token.
    """
    for name, value in (("event_id", event_id), ("command_id", command_id), ("operation_id", operation_id)):
        if not isinstance(value, str) or UUID_RE.fullmatch(value.lower()) is None:
            raise OnlineSummaryContractError(f"invalid online summary {name}")
    if environment not in {"development", "staging"} or not RUN_RE.fullmatch(run_id):
        raise OnlineSummaryContractError("invalid online summary identity")
    if ((environment == "development" and not run_id.startswith("dev-"))
            or (environment == "staging" and not run_id.startswith("STG-"))):
        raise OnlineSummaryContractError("online summary environment/run mismatch")
    if not TASK_RE.fullmatch(str(task_id)):
        raise OnlineSummaryContractError("invalid online summary task_id")
    for name, value in (("source_id", source_id), ("revision", revision), ("task_revision", task_revision)):
        if type(value) is not int or value < 1:
            raise OnlineSummaryContractError(f"invalid online summary {name}")
    if not isinstance(parser_type, str) or not PARSER_TYPE_RE.fullmatch(parser_type):
        raise OnlineSummaryContractError("invalid online summary parser_type")
    if not isinstance(business_date, str) or not DATE_RE.fullmatch(business_date):
        raise OnlineSummaryContractError("invalid online summary business_date")
    try:
        date.fromisoformat(business_date)
    except ValueError as exc:
        raise OnlineSummaryContractError("invalid online summary business_date") from exc
    if not isinstance(row_key, str) or not row_key or len(row_key) > 128:
        raise OnlineSummaryContractError("invalid online summary row_key")
    if not isinstance(occurred_at, str):
        raise OnlineSummaryContractError("invalid online summary occurred_at")
    try:
        parsed_occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OnlineSummaryContractError("invalid online summary occurred_at") from exc
    if parsed_occurred_at.tzinfo is None:
        raise OnlineSummaryContractError("online summary occurred_at must be UTC")
    occurred_at = parsed_occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(first_community, str) or not first_community.strip() or not isinstance(first_inspector, str) or not first_inspector.strip():
        raise OnlineSummaryContractError("online summary first ownership is missing")
    if task_state not in SUMMARY_STATES or activity_kind not in SUMMARY_ACTIVITY_KINDS:
        raise OnlineSummaryContractError("invalid online summary state or activity")
    if type(deleted) is not bool or type(correction) is not bool:
        raise OnlineSummaryContractError("invalid online summary event flags")
    flags = {
        "included": included,
        "online_present": online_present,
        "unable_to_verify": unable_to_verify,
        "reached_bottom": reached_bottom,
        "effective_workload": effective_workload,
    }
    if any(type(value) is not int or value < 0 for value in flags.values()):
        raise OnlineSummaryContractError("invalid online summary counters")
    if included not in {0, 1} or online_present not in {0, 1} or unable_to_verify not in {0, 1} or reached_bottom not in {0, 1}:
        raise OnlineSummaryContractError("invalid online summary presence flags")
    if effective_workload not in {0, 1}:
        raise OnlineSummaryContractError("invalid online summary effective_workload")
    if activity_kind in {"carryover", "changed"} and effective_workload != 0:
        raise OnlineSummaryContractError("online summary activity cannot add workload")
    if (deleted and activity_kind != "removed") or (not deleted and activity_kind == "removed"):
        raise OnlineSummaryContractError("deleted summary fact must be removed")
    if deleted and online_present != 0:
        raise OnlineSummaryContractError("deleted summary fact cannot be online-present")
    return {
        "contract_version": 1,
        "event_type": "summary.task.deleted" if deleted else "summary.task.changed",
        "event_id": event_id.lower(),
        "command_id": command_id.lower(),
        "operation_id": operation_id.lower(),
        "environment": environment,
        "run_id": run_id,
        "task_id": task_id,
        "row_key_hmac": summary_dimension_token(
            row_key, dimension="task_row", environment=environment, secret=secret,
        ),
        "source_id": source_id,
        "revision": revision,
        "task_revision": task_revision,
        "business_date": business_date,
        "parser_type": parser_type,
        "community_token": summary_dimension_token(
            first_community, dimension="community", environment=environment, secret=secret,
        ),
        "inspector_token": summary_dimension_token(
            first_inspector, dimension="inspector", environment=environment, secret=secret,
        ),
        "task_state": task_state,
        **flags,
        "activity_kind": activity_kind,
        "deleted": bool(deleted),
        "correction": bool(correction),
        "occurred_at": occurred_at,
    }


def summary_aggregate_key(identity: SummaryIdentity, aggregate: SummaryAggregate) -> str:
    return f"{identity.prefix}:{aggregate.key_suffix}"


def summary_index_key(
    identity: SummaryIdentity, *, business_date: str, parser_type: str,
) -> str:
    if not DATE_RE.fullmatch(business_date):
        raise OnlineSummaryContractError("invalid online summary business_date")
    try:
        date.fromisoformat(business_date)
    except ValueError as exc:
        raise OnlineSummaryContractError("invalid online summary business_date") from exc
    if not SUMMARY_PARSER_RE.fullmatch(parser_type):
        raise OnlineSummaryContractError("invalid online summary parser_type")
    return f"{identity.prefix}:index:{business_date}:{parser_type}"


def summary_watermark_key(identity: SummaryIdentity) -> str:
    return f"{identity.prefix}:watermark"


def summary_state_key(identity: SummaryIdentity) -> str:
    return f"{identity.prefix}:state"


def encode_watermark(
    *, identity: SummaryIdentity, generation: int, complete_through_business_date: str,
    source_revision: int, kafka_offsets: Mapping[str, int] | None = None,
    computed_at: str, lag_status: str = "current",
) -> dict[str, Any]:
    """Build the atomic watermark hash written after a generation is complete."""
    if type(generation) is not int or generation < 1:
        raise OnlineSummaryContractError("invalid online summary generation")
    if not DATE_RE.fullmatch(complete_through_business_date):
        raise OnlineSummaryContractError("invalid online summary watermark date")
    try:
        date.fromisoformat(complete_through_business_date)
        datetime.fromisoformat(str(computed_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise OnlineSummaryContractError("invalid online summary watermark timestamp") from exc
    if type(source_revision) is not int or source_revision < 0:
        raise OnlineSummaryContractError("invalid online summary source revision")
    offsets = dict(kafka_offsets or {})
    if any(
        not isinstance(key, str) or type(value) is not int or value < 0
        for key, value in offsets.items()
    ):
        raise OnlineSummaryContractError("invalid online summary kafka offsets")
    if lag_status not in {"current", "lagging", "rebuilding"}:
        raise OnlineSummaryContractError("invalid online summary lag status")
    return {
        "environment": identity.environment,
        "run_id": identity.run_id,
        "generation": generation,
        "complete_through_business_date": complete_through_business_date,
        "source_revision": str(source_revision),
        "kafka_offsets": json.dumps(dict(sorted(offsets.items())), separators=(",", ":")),
        "computed_at": str(computed_at),
        "lag_status": lag_status,
    }


def decode_watermark(raw: Mapping[str, Any], identity: SummaryIdentity) -> dict[str, Any]:
    if not raw or str(raw.get("environment") or "") != identity.environment or str(raw.get("run_id") or "") != identity.run_id:
        raise OnlineSummaryReadModelError("online_summary_projection_pending")
    try:
        generation = int(raw["generation"])
        source_revision = int(raw["source_revision"])
        offsets = json.loads(str(raw.get("kafka_offsets") or "{}"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OnlineSummaryReadModelError("online_summary_projection_pending") from exc
    if (
        generation < 1 or source_revision < 0
        or not isinstance(offsets, dict)
        or any(type(key) is not str or type(value) is not int or value < 0 for key, value in offsets.items())
        or str(raw.get("lag_status") or "") not in {"current", "lagging", "rebuilding"}
    ):
        raise OnlineSummaryReadModelError("online_summary_projection_pending")
    return {
        "environment": identity.environment,
        "run_id": identity.run_id,
        "generation": generation,
        "complete_through_business_date": str(raw.get("complete_through_business_date") or ""),
        "source_revision": source_revision,
        "kafka_offsets": offsets,
        "computed_at": str(raw.get("computed_at") or ""),
        "lag_status": str(raw.get("lag_status")),
    }


async def require_online_summary_ready(redis_client: Any, identity: SummaryIdentity) -> dict[str, Any]:
    """Read the exact run's state/watermark or raise a fixed 503 category."""
    try:
        state = await redis_client.get(summary_state_key(identity))
        if state != SUMMARY_STATE_READY:
            raise OnlineSummaryReadModelError("online_summary_projection_pending")
        raw = await redis_client.hgetall(summary_watermark_key(identity))
    except OnlineSummaryReadModelError:
        raise
    except Exception as exc:
        raise OnlineSummaryReadModelError("online_summary_service_unavailable") from exc
    return decode_watermark(raw, identity)


__all__ = [
    "SUMMARY_NAMESPACE", "SummaryAggregate", "SummaryIdentity",
    "OnlineSummaryContractError", "OnlineSummaryReadModelError",
    "summary_dimension_token", "summary_aggregate_key", "summary_index_key",
    "summary_fact_rebuild_reason",
    "build_online_summary_fact",
    "summary_watermark_key", "summary_state_key", "encode_watermark",
    "decode_watermark", "require_online_summary_ready",
]
