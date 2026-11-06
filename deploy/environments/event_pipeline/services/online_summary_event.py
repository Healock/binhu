"""Adapter for explicitly produced online-summary facts.

The existing task event envelope is not a summary fact.  This adapter only
accepts an explicit ``summary_fact`` member so callers cannot infer business
day, ownership, or workload from a task snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .online_summary_contract import (
    OnlineSummaryContractError,
    validate_summary_fact,
)


def summary_fact_from_bridge_event(
    event: Mapping[str, Any], *, environment: str, run_id: str,
) -> dict[str, Any] | None:
    """Return a canonical summary fact or ``None`` for unrelated events.

    A present but invalid fact raises a fixed contract error.  The adapter
    never reads or decrypts ``encrypted_payload`` from task events.
    """
    if not isinstance(event, Mapping):
        return None
    raw = event.get("summary_fact")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise OnlineSummaryContractError("invalid online summary fact envelope")
    fact = validate_summary_fact(raw)
    if fact["environment"] != environment or fact["run_id"] != run_id:
        raise OnlineSummaryContractError("online summary event run identity mismatch")
    return fact


__all__ = ["summary_fact_from_bridge_event"]
