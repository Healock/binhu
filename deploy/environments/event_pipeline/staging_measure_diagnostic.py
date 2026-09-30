#!/usr/bin/env python3
"""Read-only Staging measure diagnostic with fixed, redacted output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
BASE = Path("/var/lib/binhu-staging-event-pipeline/candidates")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    args = parser.parse_args()
    if not RUN_RE.fullmatch(args.run_id):
        raise SystemExit("staging_measure_diagnostic_refused")
    root = BASE / args.run_id
    source = root / "source" / "deploy" / "environments"
    if root.is_symlink() or root.parent != BASE or not root.is_dir() or source.is_symlink() or not source.is_dir():
        raise SystemExit("staging_measure_candidate_missing")
    sys.path.insert(0, str(source))
    from event_pipeline import staging_control

    try:
        result = staging_control.measure(args.run_id)
    except Exception as error:
        code = str(error)
        if not re.fullmatch(r"[A-Za-z0-9_. -]{1,96}", code):
            code = "measure_failed"
        print(json.dumps({
            "environment": "staging", "run_id": args.run_id,
            "status": "failed", "error_type": type(error).__name__, "error_code": code,
        }, sort_keys=True))
        raise SystemExit(1) from None
    print(json.dumps({
        "environment": "staging", "run_id": args.run_id,
        "status": "passed", "backend_network_id": result.get("backend_network_id"),
        "staging_snapshot_id": result.get("staging_snapshot_id"),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
