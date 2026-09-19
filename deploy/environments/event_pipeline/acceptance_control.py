"""Host-side fixed controller for the Dev scale acceptance runner."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from . import control
from .prepare import PROJECT, ROOT
from .scale_acceptance import validate_scale


SAFE_DETAIL_RE = re.compile(
    r"acceptance_failure_type=[A-Za-z][A-Za-z0-9_]{0,63} "
    r"acceptance_failure_stage=(?:configuration|database_connect|enqueue|compare|evidence|runtime)"
)


def write_failure_evidence(run_id: str, scale: int, returncode: int, stderr: str) -> Path:
    matches = SAFE_DETAIL_RE.findall(stderr or "")
    safe_detail = matches[-1] if matches else "acceptance_failure_type=RuntimeError acceptance_failure_stage=runtime"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = ROOT / f"acceptance-failure-{run_id}-{scale}-{stamp}.json"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "environment": "development", "run_id": run_id, "scale": scale,
            "result": "failed", "runner_exit_code": int(returncode),
            "safe_detail": safe_detail,
        }, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    return path


def execute(run_id: str, scale: int) -> dict:
    scale = validate_scale(scale)
    measured = control.measure()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    if (
        measured.get("environment") != "development"
        or measured.get("project") != PROJECT
        or measured.get("run_id") != run_id
        or manifest.get("run_id") != run_id
        or manifest.get("acceptance") != "pending"
    ):
        raise ValueError("current Dev acceptance identity mismatch")
    compose = ["docker", "compose", "-f", str(ROOT / "compose.json")]
    stopped = subprocess.run(
        [*compose, "stop", "dual-track-monitor"], capture_output=True, text=True, timeout=60
    )
    if stopped.returncode:
        raise ValueError("Dev acceptance monitor pause failed")
    result = subprocess.run(
        [*compose, "--profile", "acceptance", "run", "--rm", "--no-deps",
         "acceptance-runner", "python", "-m", "event_pipeline.scale_acceptance",
         "--scale", str(scale)],
        capture_output=True, text=True, timeout={1002: 360, 10_000: 3660, 100_000: 21_660}[scale],
    )
    if result.returncode:
        evidence = write_failure_evidence(run_id, scale, result.returncode, result.stderr)
        safe_detail = json.loads(evidence.read_text(encoding="utf-8"))["safe_detail"]
        print(safe_detail, flush=True)
        raise ValueError("Dev scale acceptance did not converge")
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise ValueError("Dev scale acceptance report missing")
    report = json.loads(lines[-1])
    if (
        report.get("environment") != "development"
        or report.get("run_id") != run_id
        or report.get("scale") != scale
        or report.get("passed") is not True
        or report.get("unattributed_difference_count") != 0
    ):
        raise ValueError("Dev scale acceptance report mismatch")
    restarted = subprocess.run(
        [*compose, "up", "-d", "--wait", "--wait-timeout", "90", "dual-track-monitor"],
        capture_output=True, text=True, timeout=120,
    )
    if restarted.returncode:
        raise ValueError("Dev acceptance monitor restart failed")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scale", type=int, choices=(1002, 10_000, 100_000), required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(execute(args.run_id, args.scale), sort_keys=True))
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise SystemExit("Dev scale acceptance refused; inspect private evidence") from None


if __name__ == "__main__":
    main()
