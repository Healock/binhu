#!/usr/bin/env python3
"""Read-only runtime status for one Staging event-pipeline candidate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
BASE = Path("/srv/binhu-environments/staging-event-pipeline")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    args = parser.parse_args()
    if not RUN_RE.fullmatch(args.run_id):
        raise SystemExit("staging_runtime_diagnostic_refused")
    root = BASE / args.run_id
    compose = root / "compose.json"
    if root.is_symlink() or root.parent != BASE or not root.is_dir() or compose.is_symlink() or not compose.is_file():
        raise SystemExit("staging_runtime_candidate_missing")
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "ps", "-a", "--format", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        print(json.dumps({"environment": "staging", "run_id": args.run_id,
                          "status": "failed", "error_code": "docker_ps_failed"}, sort_keys=True))
        raise SystemExit(1)
    services = []
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        service, state, health, exit_code = (value.get("Service"), value.get("State"),
                                              value.get("Health"), value.get("ExitCode"))
        if (isinstance(service, str) and re.fullmatch(r"[a-z0-9-]{1,64}", service)
                and isinstance(state, str) and re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", state)
                and (health is None or (isinstance(health, str) and re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", health)))
                and (exit_code is None or (isinstance(exit_code, int) and -255 <= exit_code <= 255))):
            services.append({"service": service, "state": state, "health": health, "exit_code": exit_code})
    print(json.dumps({"environment": "staging", "run_id": args.run_id,
                      "status": "passed", "services": services}, sort_keys=True))


if __name__ == "__main__":
    main()
