"""Shared fixed Production safety thresholds for isolated load-test monitors."""
from __future__ import annotations

from typing import Any
import re


HEALTH_FAILURE_LIMIT = 2
NEW_ERROR_LIMIT = 0
PRODUCTION_CONTAINERS = ("binhu-backend", "binhu-mysql", "binhu-ops-agent")
PRODUCTION_MODE_RUNNING = "running"
PRODUCTION_MODE_PAUSED = "production_paused"
PAUSE_RUN_RE = re.compile(r"^PROD-PAUSE-[0-9]{8}-[0-9]{2}$")
AUTHORIZED_PAUSE_RUN_ID = "PROD-PAUSE-20261029-02"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PRODUCTION_PAUSE_DATABASES = (
    "DispatchData", "OnlineData", "OnlineDataArchive", "PlatformData",
    "RegistryData", "VisitData", "WorkflowData", "daily_report",
)


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


def _paused_state_reason(states: dict[str, dict[str, Any]]) -> str:
    if not isinstance(states, dict) or set(states) != set(PRODUCTION_CONTAINERS):
        return "production_monitor_baseline_incomplete"
    if any(not isinstance(state, dict) for state in states.values()):
        return "production_container_inspection_failed"
    if any(state.get("error") for state in states.values()):
        return "production_container_inspection_failed"
    for name, state in states.items():
        if state.get("oom_killed") or state.get("restarting"):
            return "production_paused_container_unhealthy"
        expected_running = name == "binhu-ops-agent"
        if bool(state.get("running")) != expected_running:
            return "production_paused_state_changed"
        if expected_running and state.get("health") == "unhealthy":
            return "production_paused_container_unhealthy"
    return ""


class ProductionStopMonitor:
    def __init__(self, baseline: dict[str, Any], *, mode: str = PRODUCTION_MODE_RUNNING,
                 pause_run_id: str | None = None):
        if mode not in {PRODUCTION_MODE_RUNNING, PRODUCTION_MODE_PAUSED}:
            raise ValueError("production_monitor_mode_invalid")
        self.mode = mode
        self.pause_run_id = pause_run_id
        if mode == PRODUCTION_MODE_PAUSED:
            self._init_paused(baseline, pause_run_id)
            return
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

    def _init_paused(self, baseline: dict[str, Any], pause_run_id: str | None) -> None:
        if (not isinstance(pause_run_id, str) or not PAUSE_RUN_RE.fullmatch(pause_run_id)
                or pause_run_id != AUTHORIZED_PAUSE_RUN_ID):
            raise ValueError("production_pause_run_id_invalid")
        if (baseline.get("environment") != "production"
                or baseline.get("bootstrap_environment") != "production"
                or baseline.get("production_mode") != PRODUCTION_MODE_PAUSED
                or baseline.get("pause_run_id") != pause_run_id
                or baseline.get("pause_evidence_valid") is not True):
            raise ValueError("production_pause_evidence_missing")
        if (not SHA256_RE.fullmatch(str(baseline.get("pause_evidence_sha256") or ""))
                or not SHA256_RE.fullmatch(str(baseline.get("backup_manifest_sha256") or ""))):
            raise ValueError("production_pause_evidence_invalid")
        if baseline.get("backup_database_count") != len(PRODUCTION_PAUSE_DATABASES):
            raise ValueError("production_pause_backup_scope_mismatch")
        if baseline.get("backup_database_scope") != list(PRODUCTION_PAUSE_DATABASES):
            raise ValueError("production_pause_backup_scope_mismatch")
        states = baseline.get("containers")
        reason = _paused_state_reason(states)
        if reason:
            raise ValueError("production_monitor_baseline_" + reason)
        if baseline.get("monitor_error") is not None:
            raise ValueError("production_pause_evidence_invalid")
        errors = baseline.get("error_count")
        if not isinstance(errors, int) or errors < 0:
            raise ValueError("production_error_monitor_unavailable")
        self.baseline_restarts = {
            name: int(state.get("restart_count") or 0) for name, state in states.items()
        }
        self.baseline_errors = errors
        self.pause_evidence_sha256 = str(baseline["pause_evidence_sha256"])
        self.backup_manifest_sha256 = str(baseline["backup_manifest_sha256"])
        self.health_failures = 0

    def observe(self, production: dict[str, Any]) -> str:
        if self.mode == PRODUCTION_MODE_PAUSED:
            return self._observe_paused(production)
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

    def _observe_paused(self, production: dict[str, Any]) -> str:
        if (not isinstance(production, dict)
                or production.get("environment") != "production"
                or production.get("bootstrap_environment") != "production"
                or production.get("production_mode") != PRODUCTION_MODE_PAUSED
                or production.get("pause_run_id") != self.pause_run_id):
            return "production_pause_evidence_mismatch"
        if production.get("pause_evidence_valid") is not True:
            return "production_pause_evidence_missing"
        if (production.get("pause_evidence_sha256") != self.pause_evidence_sha256
                or production.get("backup_manifest_sha256") != self.backup_manifest_sha256):
            return "production_pause_evidence_mismatch"
        if production.get("backup_database_count") != len(PRODUCTION_PAUSE_DATABASES):
            return "production_pause_backup_scope_mismatch"
        if production.get("backup_database_scope") != list(PRODUCTION_PAUSE_DATABASES):
            return "production_pause_backup_scope_mismatch"
        states = production.get("containers")
        reason = _paused_state_reason(states)
        if reason:
            return reason
        for name, state in states.items():
            try:
                restart_count = int(state.get("restart_count") or 0)
            except (TypeError, ValueError):
                return "production_container_inspection_failed"
            if restart_count != self.baseline_restarts.get(name):
                return "production_paused_state_changed"
        current_errors = production.get("error_count")
        if not isinstance(current_errors, int) or current_errors < self.baseline_errors:
            return "production_error_monitor_unavailable"
        if current_errors > self.baseline_errors + NEW_ERROR_LIMIT:
            return "production_new_error_threshold_exceeded"
        return ""
