"""Merge independently collected Staging and Production read-only samples."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _json_lines(path: str | Path, label: str) -> list[dict[str, Any]]:
    target = Path(path)
    if target.is_symlink() or not target.is_file() or target.stat().st_size > 32 * 1024 * 1024:
        raise RuntimeError(f"{label} sample file invalid")
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise RuntimeError(f"{label} sample is not an object")
        rows.append(item)
    if len(rows) < 2:
        raise RuntimeError(f"{label} requires at least two samples")
    return rows


def merge_samples(run_id: str, staging_path: str | Path, production_path: str | Path) -> dict[str, Any]:
    staging = _json_lines(staging_path, "Staging")
    production = _json_lines(production_path, "Production")
    if len(staging) != len(production):
        raise RuntimeError("Staging and Production sample counts differ")
    combined: list[dict[str, Any]] = []
    for index, (stage, prod) in enumerate(zip(staging, production)):
        if (stage.get("run_id") != run_id or stage.get("environment") != "staging"
                or stage.get("production_data") is not False):
            raise RuntimeError("Staging sample identity mismatch")
        if (prod.get("environment") != "production" or prod.get("read_only") is not True
                or prod.get("business_data_included") is not False):
            raise RuntimeError("Production read-only sample identity mismatch")
        metrics = prod.get("metrics")
        required = {"healthy", "restart_count", "oom_killed_count", "error_count"}
        if not isinstance(metrics, dict) or not required.issubset(metrics):
            raise RuntimeError("Production read-only metrics incomplete")
        stage_time = int(stage.get("sampled_at_unix") or 0)
        prod_time = int(prod.get("sampled_at_unix") or 0)
        if stage_time <= 0 or abs(stage_time - prod_time) > 90:
            raise RuntimeError(f"sample {index} observation windows do not align")
        item = dict(stage)
        item["production"] = {key: metrics[key] for key in sorted(required)}
        combined.append(item)
    return {
        "run_id": run_id,
        "environment": "staging",
        "production_data": False,
        "samples": combined,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--staging", required=True)
    parser.add_argument("--production", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = merge_samples(args.run_id, args.staging, args.production)
    target = Path(args.output)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
