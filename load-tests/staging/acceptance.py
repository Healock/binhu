"""Assemble the final Staging promotion report from bounded evidence files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .report import evaluate_report
from .resource_metrics import summarize_resource_samples


def _read_json(path: str | Path, label: str) -> Any:
    target = Path(path)
    if target.is_symlink() or not target.is_file() or target.stat().st_size > 16 * 1024 * 1024:
        raise RuntimeError(f"{label} evidence is missing or invalid")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError(f"{label} evidence is unreadable") from None


def _identity(payload: dict[str, Any], run_id: str, label: str) -> None:
    if payload.get("run_id") != run_id or payload.get("environment") != "staging":
        raise RuntimeError(f"{label} identity mismatch")
    if payload.get("production_data") is not False:
        raise RuntimeError(f"{label} must exclude Production data")


def assemble_report(
    *,
    run_id: str,
    load_report_path: str | Path,
    resource_samples_path: str | Path,
    rollback_report_path: str | Path,
    manual_report_path: str | Path,
) -> dict[str, Any]:
    load = _read_json(load_report_path, "load")
    if not isinstance(load, dict):
        raise RuntimeError("load evidence must be an object")
    _identity(load, run_id, "load")
    execution = load.get("execution_policy")
    memory_gate = execution.get("memory_gate") if isinstance(execution, dict) else None
    if (not isinstance(execution, dict) or execution.get("users") != 75
            or execution.get("production_monitor") != "passed"
            or not isinstance(memory_gate, dict)
            or memory_gate.get("required_bytes") != 4 * 1024 ** 3
            or not isinstance(memory_gate.get("waived"), bool)
            or not isinstance(memory_gate.get("observed_bytes"), int)):
        raise RuntimeError("load execution policy evidence is missing or invalid")
    if memory_gate["observed_bytes"] < memory_gate["required_bytes"] and not memory_gate["waived"]:
        raise RuntimeError("load memory gate was below threshold without a recorded waiver")

    samples_envelope = _read_json(resource_samples_path, "resource")
    if not isinstance(samples_envelope, dict):
        raise RuntimeError("resource evidence must be an object")
    _identity(samples_envelope, run_id, "resource")
    samples = samples_envelope.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        raise RuntimeError("resource evidence requires at least two samples")

    rollback = _read_json(rollback_report_path, "rollback")
    manual = _read_json(manual_report_path, "manual")
    if not isinstance(rollback, dict) or not isinstance(manual, dict):
        raise RuntimeError("rollback and manual evidence must be objects")
    _identity(rollback, run_id, "rollback")
    _identity(manual, run_id, "manual")

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
    limited_acceptance = bool(memory_gate["waived"])
    checks_passed = threshold_result["passed"] and rollback_passed and manual_passed
    return {
        "run_id": run_id,
        "environment": "staging",
        "production_data": False,
        "load": load,
        "resources": resources,
        "rollback": rollback,
        "manual_acceptance": manual,
        "thresholds": threshold_result,
        "execution_policy": execution,
        "limited_acceptance": limited_acceptance,
        "acceptance_limitations": ([
            "The 4 GiB preflight memory gate was explicitly waived for this run.",
            "This run does not satisfy the original 4 GiB resource precondition or constitute full architecture acceptance.",
        ] if limited_acceptance else []),
        "rollback_passed": rollback_passed,
        "manual_acceptance_passed": manual_passed,
        "passed": checks_passed and not limited_acceptance,
        "promotion_ready": checks_passed and not limited_acceptance,
    }
