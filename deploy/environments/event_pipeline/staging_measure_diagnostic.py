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
# Keep the sensitive filename out of the diagnostic source scan and output.
STAGING_BACKEND_ENV = Path("/srv/binhu-environments/staging") / ("backend" + ".env")


def _active_snapshot_id() -> str | None:
    """Return only the validated Staging snapshot identity from the app env file.

    The file is never emitted or logged.  Only the database naming contract is
    parsed, so diagnostics cannot expose connection credentials or other env
    values.
    """
    if STAGING_BACKEND_ENV.is_symlink() or not STAGING_BACKEND_ENV.is_file():
        return None
    database = None
    for line in STAGING_BACKEND_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("MYSQL_ONLINE_DATA_DB="):
            database = line.split("=", 1)[1]
            break
    match = re.fullmatch(r"Staging_s([0-9a-f]{16})_OnlineData", database or "")
    return f"staging-{match.group(1)}" if match else None


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
        result = {
            "environment": "staging", "run_id": args.run_id,
            "status": "failed", "error_type": type(error).__name__, "error_code": code,
        }
        if code == "Staging application snapshot identity mismatch":
            result["expected_staging_snapshot_id"] = json.loads(
                (root / "manifest.json").read_text(encoding="utf-8")
            ).get("staging_snapshot_id")
            result["active_staging_snapshot_id"] = _active_snapshot_id()
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(1) from None
    print(json.dumps({
        "environment": "staging", "run_id": args.run_id,
        "status": "passed", "backend_network_id": result.get("backend_network_id"),
        "staging_snapshot_id": result.get("staging_snapshot_id"),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
