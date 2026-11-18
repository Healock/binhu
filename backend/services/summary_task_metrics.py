"""Shared arithmetic for online summary task counts.

Source adapters determine identity, state, and date classification; this module
keeps metric arithmetic consistent across local and read-only external sources.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

SUMMARY_METRIC_KEYS = (
    "total", "carryover", "new", "changed", "pending", "unchecked",
    "checked", "completed", "unable_to_verify",
)


def _empty_metrics(source: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {key: 0 for key in SUMMARY_METRIC_KEYS}
    if source:
        result["sources"] = [source]
    return result


def aggregate_summary_task_facts(
    facts: Iterable[Mapping[str, Any]],
    *,
    source: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    """Aggregate normalized task facts by community and checker."""
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: _empty_metrics(source))
    by_checker: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: _empty_metrics(source))
    )
    for fact in facts:
        community = str(fact.get("community") or "")
        checker = str(fact.get("checker") or "")
        state = str(fact.get("state") or "unchecked")
        count = max(0, int(fact.get("count") or 0))
        if not count:
            continue
        for metrics in (totals[community], by_checker[community][checker]):
            metrics["total"] += count
            if bool(fact.get("unable_to_verify")):
                metrics["unable_to_verify"] += count
            if state == "completed":
                metrics["completed"] += count
            else:
                metrics["pending"] += count
                metrics["checked" if state == "checked" else "unchecked"] += count
            if bool(fact.get("carryover")) and state != "completed":
                metrics["carryover"] += count
            if bool(fact.get("new")):
                metrics["new"] += count
            if bool(fact.get("changed")):
                metrics["changed"] += count
    return dict(totals), {
        community: dict(checkers)
        for community, checkers in by_checker.items()
    }
