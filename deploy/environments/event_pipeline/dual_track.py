"""Run and compare Dev Python/Flink task metadata projections.

The command consumes two JSONL files produced by isolated workers.  It writes
an immutable, redacted comparison report and never connects to Production or
Staging.  A separate runner can feed the files from Kafka/Flink inspectors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .services.task_metadata_projection import flatten_projection, project_events


RUN_RE = re.compile(r"^dev-[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
EVIDENCE_RE = re.compile(r"^dual-track-[0-9]{8}-[A-Za-z0-9_-]{4,48}$")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("projection input must be a regular file")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("projection input row must be an object")
        rows.append(value)
    return rows


def _safe_row(value: dict[str, Any]) -> dict[str, Any]:
    # Only the comparison fields are written to the report.  This deliberately
    # excludes any accidental event body, person, address or free text.
    return flatten_projection(value)


def compare(python_path: Path, flink_path: Path, run_id: str, evidence_id: str, output: Path) -> dict[str, Any]:
    if os.environ.get("APP_ENVIRONMENT", "development") != "development":
        raise ValueError("dual-track comparison requires development identity")
    if not RUN_RE.fullmatch(run_id):
        raise ValueError("invalid Dev run ID")
    if not EVIDENCE_RE.fullmatch(evidence_id):
        raise ValueError("fresh dual-track evidence ID required")
    if output.exists() or output.is_symlink():
        raise ValueError("comparison output must be new")
    python_events = _read_jsonl(python_path)
    flink_events = _read_jsonl(flink_path)
    python_result = project_events(python_events)
    flink_result = project_events(flink_events)
    all_keys = sorted(set(python_result) | set(flink_result))
    differences = []
    for key in all_keys:
        expected = _safe_row(python_result[key]) if key in python_result else None
        actual = _safe_row(flink_result[key]) if key in flink_result else None
        if expected != actual:
            task_id = key[1]
            differences.append({
                "task_id_sha256": hashlib.sha256(task_id.encode()).hexdigest(),
                "revision": (actual or expected or {}).get("revision", 0),
                "fields": sorted(set((expected or {}) | (actual or {}))),
                "expected": expected,
                "actual": actual,
                "attribution": "missing_in_track" if expected is None or actual is None else "projection_value_mismatch",
            })
    report = {
        "environment": "development",
        "run_id": run_id,
        "evidence_id": evidence_id,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python_tasks": len(python_result),
        "flink_tasks": len(flink_result),
        "unique_events_python": len(python_events),
        "unique_events_flink": len(flink_events),
        "differences": differences,
        "unattributed_difference_count": len(differences),
        "passed": not differences,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", dest="python_path", type=Path, required=True)
    parser.add_argument("--flink", dest="flink_path", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compare(args.python_path, args.flink_path, args.run_id, args.evidence_id, args.output)
        print(json.dumps({"passed": result["passed"], "unattributed_difference_count": result["unattributed_difference_count"]}))
        if not result["passed"]:
            raise SystemExit(2)
    except (OSError, ValueError, json.JSONDecodeError):
        raise SystemExit("Dev dual-track comparison failed; preserve redacted evidence") from None
