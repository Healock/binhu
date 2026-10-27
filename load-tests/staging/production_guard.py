"""Shared fixed Production safety thresholds for isolated load-test monitors."""
from __future__ import annotations

from typing import Any


HEALTH_FAILURE_LIMIT = 2
NEW_ERROR_LIMIT = 0
PRODUCTION_CONTAINERS = ("binhu-backend", "binhu-mysql", "binhu-ops-agent")


def production_stop_reason(
    *,
    health_failures: int,
    states: dict[str, dict[str, Any]],
    baseline_restarts: dict[str, int],
    new_error_count: int | None,
) -> str:
    """Fail closed when fixed Production health, container, or error checks fail."""
    if health_failures >= HEALTH_FAILURE_LIMIT:
        return "production_health_failed_twice"
    if not states or set(states) != set(baseline_restarts):
        return "production_container_monitor_incomplete"
    if any("error" in state for state in states.values()):
        return "production_container_inspection_failed"
    if any(
        state.get("oom_killed")
        or state.get("restarting")
        or not state.get("running", False)
        or state.get("health") == "unhealthy"
        or int(state.get("restart_count") or 0) > baseline_restarts.get(name, -1)
        for name, state in states.items()
    ):
        return "production_container_unhealthy_or_restarted"
    if new_error_count is not None and new_error_count > NEW_ERROR_LIMIT:
        return "production_new_error_threshold_exceeded"
    return ""


class ProductionStopMonitor:
    def __init__(self, baseline: dict[str, Any]):
        if (baseline.get("environment") != "production"
                or baseline.get("bootstrap_environment") != "production"
                or baseline.get("health") is not True
                or baseline.get("monitor_error") is not None):
            raise ValueError("production_monitor_baseline_invalid")
        states = baseline.get("containers")
        if not isinstance(states, dict) or set(states) != set(PRODUCTION_CONTAINERS):
            raise ValueError("production_monitor_baseline_incomplete")
        self.baseline_restarts = {
            name: int(state.get("restart_count") or 0) for name, state in states.items()
        }
        self.baseline_errors = int(baseline.get("error_count") or 0)
        self.health_failures = 0
        reason = production_stop_reason(
            health_failures=0,
            states=states,
            baseline_restarts=self.baseline_restarts,
            new_error_count=0,
        )
        if reason:
            raise ValueError("production_monitor_baseline_" + reason)

    def observe(self, production: dict[str, Any]) -> str:
        if (not isinstance(production, dict)
                or production.get("environment") != "production"
                or production.get("bootstrap_environment") != "production"
                or production.get("monitor_error") is not None):
            return "production_monitor_unavailable"
        health = production.get("health")
        if not isinstance(health, bool):
            return "production_health_monitor_unavailable"
        self.health_failures = self.health_failures + 1 if not health else 0
        states = production.get("containers")
        if not isinstance(states, dict):
            return "production_container_monitor_incomplete"
        current_errors = production.get("error_count")
        if not isinstance(current_errors, int) or current_errors < self.baseline_errors:
            return "production_error_monitor_unavailable"
        return production_stop_reason(
            health_failures=self.health_failures,
            states=states,
            baseline_restarts=self.baseline_restarts,
            new_error_count=current_errors - self.baseline_errors,
        )
