#!/usr/bin/env python3
"""Read-only, redacted diagnostics for one Schema Registry preload."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
ROOT = Path("/var/lib/binhu-staging-event-pipeline/evidence")
SAFE = ("environment", "run_id", "source_environment", "image_key", "expected_image_id",
        "loaded_image_id", "status", "error_code", "archive_sha256", "archive_size", "started")


def main() -> None:
    if len(sys.argv) != 2 or not RUN_RE.fullmatch(sys.argv[1]):
        raise SystemExit("staging_preload_diagnostic_refused")
    run_id = sys.argv[1]
    path = ROOT / run_id / "schema-registry-preload.json"
    if path.is_symlink() or not path.is_file() or path.parent.parent != ROOT:
        print(json.dumps({"environment": "staging", "run_id": run_id,
                          "status": "failed", "error_code": "preload_evidence_missing"}, sort_keys=True))
        raise SystemExit(1)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        print(json.dumps({"environment": "staging", "run_id": run_id,
                          "status": "failed", "error_code": "preload_evidence_invalid"}, sort_keys=True))
        raise SystemExit(1)
    if not isinstance(payload, dict) or payload.get("environment") != "staging" or payload.get("run_id") != run_id:
        print(json.dumps({"environment": "staging", "run_id": run_id,
                          "status": "failed", "error_code": "preload_evidence_identity_mismatch"}, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps({key: payload[key] for key in SAFE if key in payload}, sort_keys=True))


if __name__ == "__main__":
    main()
