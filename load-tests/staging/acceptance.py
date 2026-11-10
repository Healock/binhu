"""Assemble the final Staging promotion report from bounded evidence files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .report import evaluate_report
from .resource_metrics import summarize_resource_samples
from .profile import get_profile, profile_payload


def _read_json(path: str | Path, label: str) -> Any:
    target = Path(path)
    if target.is_symlink() or not target.is_file() or target.stat().st_size > 16 * 1024 * 1024:
        raise RuntimeError(f"{label} evidence is missing or invalid")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError(f"{label} evidence is unreadable") from None


def _identity(payload: dict[str, Any], run_id: str, label: str,
              candidate_run_id: str | None = None, *, require_candidate: bool = False) -> None:
    if payload.get("run_id") != run_id or payload.get("environment") != "staging":
        raise RuntimeError(f"{label} identity mismatch")
    if payload.get("production_data") is not False:
        raise RuntimeError(f"{label} must exclude Production data")
    if candidate_run_id is not None:
        observed = payload.get("candidate_run_id")
        if (require_candidate and observed != candidate_run_id) or (observed is not None and observed != candidate_run_id):
            raise RuntimeError(f"{label} candidate identity mismatch")


def assemble_report(
    *,
    run_id: str,
    candidate_run_id: str | None = None,
    load_report_path: str | Path,
    resource_samples_path: str | Path,
    rollback_report_path: str | Path,
    manual_report_path: str | Path,
) -> dict[str, Any]:
    candidate_run_id = candidate_run_id or run_id
    require_candidate_identity = candidate_run_id != run_id
    load = _read_json(load_report_path, "load")
    if not isinstance(load, dict):
        raise RuntimeError("load evidence must be an object")
    _identity(load, run_id, "load", candidate_run_id, require_candidate=require_candidate_identity)
    execution = load.get("execution_policy")
    memory_gate = execution.get("memory_gate") if isinstance(execution, dict) else None
    profile = execution.get("workload_profile") if isinstance(execution, dict) else None
    expected_profile = (
        profile_payload(get_profile(profile.get("name", "")))
        if isinstance(profile, dict) and profile.get("name") in {"full75", "low20"}
        else None
    )
    if (not isinstance(execution, dict) or not isinstance(profile, dict)
            or profile != expected_profile
            or execution.get("users") != profile.get("users")
            or execution.get("production_monitor") != "passed"
            or not isinstance(memory_gate, dict)
            or memory_gate.get("required_bytes") != 4 * 1024 ** 3
            or not isinstance(memory_gate.get("waived"), bool)
            or (memory_gate.get("waived") and (
                memory_gate.get("waiver_requested") is not True
                or execution.get("memory_gate_approval_basis") != (
                    "user-approved low20 profile" if not profile["equivalent_to_full_75_user_gate"]
                    else "explicit workflow dispatch approval"
                )
            ))
            or not isinstance(memory_gate.get("observed_bytes"), int)):
        raise RuntimeError("load execution policy evidence is missing or invalid")
    if memory_gate["observed_bytes"] < memory_gate["required_bytes"] and not memory_gate["waived"]:
        raise RuntimeError("load memory gate was below threshold without a recorded waiver")

    samples_envelope = _read_json(resource_samples_path, "resource")
    if not isinstance(samples_envelope, dict):
        raise RuntimeError("resource evidence must be an object")
    _identity(samples_envelope, run_id, "resource", candidate_run_id,
              require_candidate=require_candidate_identity)
    samples = samples_envelope.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        raise RuntimeError("resource evidence requires at least two samples")

    rollback = _read_json(rollback_report_path, "rollback")
    manual = _read_json(manual_report_path, "manual")
    if not isinstance(rollback, dict) or not isinstance(manual, dict):
        raise RuntimeError("rollback and manual evidence must be objects")
    _identity(rollback, run_id, "rollback", candidate_run_id)
    _identity(manual, run_id, "manual", candidate_run_id)

    resources = summarize_resource_samples(samples)
    combined = {**load, "resources": resources}
    threshold_result = evaluate_report(combined)
    rollback_passed = (
        rollback.get("status") == "passed"
        and rollback.get("python_worker_restored") is True
        and rollback.get("data_consistency_verified") is True
        and rollback.get("legacy_path_healthy") is True
        and isinstance(rollback.get("elapsed_seconds"), (int, float))
        and 0 <= float(rollback["elapsed_seconds"]) <= 3600
    )
    manual_checks = manual.get("checks")
    manual_passed = (
        manual.get("status") == "passed"
        and isinstance(manual_checks, dict)
        and bool(manual_checks)
        and all(value is True for value in manual_checks.values())
    )
    limited_acceptance = bool(memory_gate["waived"] or not profile["equivalent_to_full_75_user_gate"])
    checks_passed = threshold_result["passed"] and rollback_passed and manual_passed
    return {
        "run_id": run_id,
        "load_run_id": run_id,
        "candidate_run_id": candidate_run_id,
        "environment": "staging",
        "production_data": False,
        "load": load,
        "resources": resources,
        "rollback": rollback,
        "manual_acceptance": manual,
        "thresholds": threshold_result,
        "execution_policy": execution,
        "limited_acceptance": limited_acceptance,
        "acceptance_limitations": (
            (["The 4 GiB preflight memory gate was explicitly waived for this run.",
              "This run does not satisfy the original 4 GiB resource precondition."] if memory_gate["waived"] else [])
            + (["The low20 profile is not equivalent to the 75-user Staging load gate."]
               if not profile["equivalent_to_full_75_user_gate"] else [])
        ),
        "load_profile_passed": checks_passed,
        "load_profile": profile,
        "rollback_passed": rollback_passed,
        "manual_acceptance_passed": manual_passed,
        "passed": checks_passed and not limited_acceptance,
        "promotion_ready": checks_passed and not limited_acceptance,
    }
