"""Fail-closed runtime monitor for the fixed Staging realistic-load workflow."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from .production_guard import (
    PAUSE_RUN_RE,
    PRODUCTION_MODE_PAUSED,
    PRODUCTION_MODE_RUNNING,
    ProductionStopMonitor,
)


GIB = 1024 ** 3
MEMORY_GATE_BYTES = 4 * GIB
STAGING_SERVICES = {"binhu-staging-backend-1", "binhu-staging-environment-mysql-1"}


def _read_sample(path: Path, run_id: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("staging_sample_missing_or_invalid")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("environment") != "staging"
            or payload.get("run_id") != run_id or payload.get("production_data") is not False):
        raise ValueError("staging_sample_identity_mismatch")
    if not isinstance(payload.get("host", {}).get("memory_available_bytes"), int):
        raise ValueError("staging_host_memory_sample_missing")
    return payload


def _staging_baseline(sample: dict[str, Any]) -> dict[str, Any]:
    services = sample.get("staging_services")
    pipeline = sample.get("containers")
    if not isinstance(services, dict) or set(services) != STAGING_SERVICES:
        raise ValueError("staging_application_monitor_incomplete")
    if not isinstance(pipeline, dict) or not isinstance(pipeline.get("count"), int):
        raise ValueError("staging_pipeline_monitor_incomplete")
    if (any(not item.get("running") or item.get("restarting") or item.get("oom_killed")
            for item in services.values())
            or pipeline.get("running_count") != pipeline.get("count")
            or pipeline.get("oom_killed_count") != 0):
        raise ValueError("staging_baseline_unhealthy")
    return {"services": services, "pipeline": pipeline}


def preflight(sample: dict[str, Any], *, run_id: str, waive_memory_gate: bool,
              production_mode: str = PRODUCTION_MODE_RUNNING,
              production_pause_run_id: str | None = None) -> dict[str, Any]:
    if sample.get("run_id") != run_id or sample.get("environment") != "staging":
        raise ValueError("staging_sample_identity_mismatch")
    if production_mode not in {PRODUCTION_MODE_RUNNING, PRODUCTION_MODE_PAUSED}:
        raise ValueError("production_monitor_mode_invalid")
    if production_mode == PRODUCTION_MODE_PAUSED:
        if not isinstance(production_pause_run_id, str) or not PAUSE_RUN_RE.fullmatch(production_pause_run_id):
            raise ValueError("production_pause_run_id_invalid")
    elif production_pause_run_id is not None:
        raise ValueError("production_pause_run_id_unexpected")
    baseline = _staging_baseline(sample)
    production = sample.get("production")
    ProductionStopMonitor(
        production if isinstance(production, dict) else {},
        mode=production_mode,
        pause_run_id=production_pause_run_id,
    )
    available = int(sample["host"]["memory_available_bytes"])
    breached = available < MEMORY_GATE_BYTES
    if breached and not waive_memory_gate:
        raise ValueError("staging_memory_gate_not_waived")
    return {
        "run_id": run_id,
        "environment": "staging",
        "production_data": False,
        "memory_gate": {
            "required_bytes": MEMORY_GATE_BYTES,
            "observed_bytes": available,
            "waiver_requested": waive_memory_gate,
            "waived": breached and waive_memory_gate,
            "status": "waived" if breached and waive_memory_gate else "passed",
        },
        "production_baseline": production,
        "production_mode": production_mode,
        "production_pause_run_id": production_pause_run_id,
        "production_health_failures": 0,
        "staging_baseline": baseline,
        "request_failure_started_at": None,
        "last_request_count": 0,
        "last_request_progress_at": time.time(),
    }


def _locust_counts(path: Path) -> tuple[int, int] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("locust_statistics_invalid")
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows = [row for row in rows if row.get("Name") != "Aggregated"]
    requests = sum(int(row.get("Request Count") or 0) for row in rows)
    failures = sum(int(row.get("Failure Count") or 0) for row in rows)
    # Permission-scoped fixtures intentionally exercise roles that receive
    # 401/403 on endpoints outside their scope. Keep those denials visible in
    # Locust artifacts, but exclude them from the infrastructure failure fuse.
    failure_path = path.with_name(path.name.replace("_stats", "_failures"))
    expected_auth = 0
    if failure_path.is_file() and not failure_path.is_symlink():
        with failure_path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                error = str(row.get("Error") or "")
                if re.search(r"(?:401|403)\b", error):
                    expected_auth += int(row.get("Occurrences") or 0)
    return requests, max(0, failures - expected_auth)


def _staging_stop_reason(sample: dict[str, Any], baseline: dict[str, Any]) -> str:
    services = sample.get("staging_services")
    pipeline = sample.get("containers")
    if not isinstance(services, dict) or set(services) != STAGING_SERVICES:
        return "staging_application_monitor_incomplete"
    if not isinstance(pipeline, dict):
        return "staging_pipeline_monitor_incomplete"
    for name, before in baseline["services"].items():
        current = services.get(name)
        if (not isinstance(current, dict) or not current.get("running")
                or current.get("restarting") or current.get("oom_killed")
                or int(current.get("restart_count") or 0) > int(before.get("restart_count") or 0)):
            return "staging_application_service_unhealthy_or_restarted"
    before_pipeline = baseline["pipeline"]
    if (pipeline.get("count") != before_pipeline.get("count")
            or pipeline.get("running_count") != pipeline.get("count")
            or int(pipeline.get("restart_count") or 0) > int(before_pipeline.get("restart_count") or 0)
            or int(pipeline.get("oom_killed_count") or 0) > int(before_pipeline.get("oom_killed_count") or 0)):
        return "staging_pipeline_container_unhealthy_or_restarted"
    return ""


def observe(sample: dict[str, Any], state: dict[str, Any], *, locust_stats: Path) -> tuple[dict[str, Any], str]:
    production = sample.get("production")
    baseline = state.get("production_baseline")
    try:
        tracker = ProductionStopMonitor(
            baseline if isinstance(baseline, dict) else {},
            mode=state.get("production_mode", PRODUCTION_MODE_RUNNING),
            pause_run_id=state.get("production_pause_run_id"),
        )
    except (TypeError, ValueError):
        return state, "production_monitor_baseline_invalid"
    tracker.health_failures = int(state.get("production_health_failures") or 0)
    production_reason = tracker.observe(production if isinstance(production, dict) else {})
    state["production_health_failures"] = tracker.health_failures
    if production_reason:
        return state, production_reason
    staging_reason = _staging_stop_reason(sample, state.get("staging_baseline") or {})
    if staging_reason:
        return state, staging_reason

    counts = _locust_counts(locust_stats)
    now = time.time()
    if counts is not None:
        requests, failures = counts
        if requests > int(state.get("last_request_count") or 0):
            state["last_request_count"] = requests
            state["last_request_progress_at"] = now
        if requests >= 100 and now - float(state.get("last_request_progress_at") or now) > 30:
            return state, "staging_requests_stalled_for_30_seconds"
        if requests >= 100 and failures / requests > .02:
            state["request_failure_started_at"] = state.get("request_failure_started_at") or now
            if now - float(state["request_failure_started_at"]) >= 60:
                return state, "staging_request_failure_rate_above_2_percent_for_60_seconds"
        else:
            state["request_failure_started_at"] = None
    return state, ""


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("monitor_evidence_missing_or_invalid")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("monitor_evidence_invalid")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preflight", "observe"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--waive-memory-gate", choices=("true", "false"), default="false")
    parser.add_argument("--production-mode", choices=(PRODUCTION_MODE_RUNNING, PRODUCTION_MODE_PAUSED),
                        default=PRODUCTION_MODE_RUNNING)
    parser.add_argument("--production-pause-run-id")
    parser.add_argument("--locust-stats", type=Path)
    parser.add_argument("--stop-reason", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"STG-[0-9]{8}-[0-9]{2}", args.run_id):
        raise SystemExit("staging_monitor_run_id_invalid")
    sample = _read_sample(args.sample, args.run_id)
    if args.action == "preflight":
        state = preflight(
            sample, run_id=args.run_id, waive_memory_gate=args.waive_memory_gate == "true",
            production_mode=args.production_mode,
            production_pause_run_id=args.production_pause_run_id,
        )
        args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"run_id": args.run_id, "memory_gate": state["memory_gate"],
                          "production_monitor": "ready"}, sort_keys=True))
        return
    if args.locust_stats is None or args.stop_reason is None:
        raise SystemExit("staging_monitor_arguments_missing")
    state = _load_json(args.state)
    if state.get("run_id") != args.run_id or state.get("environment") != "staging":
        raise SystemExit("staging_monitor_state_identity_mismatch")
    state, reason = observe(sample, state, locust_stats=args.locust_stats)
    args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if reason:
        args.stop_reason.write_text(json.dumps({"run_id": args.run_id, "environment": "staging",
                                               "stopped": True, "reason": reason}, indent=2) + "\n",
                                    encoding="utf-8")
        raise SystemExit(3)
    print(json.dumps({"run_id": args.run_id, "stopped": False}, sort_keys=True))


if __name__ == "__main__":
    main()
