"""Host-side fixed controller for the Dev scale acceptance runner."""
from __future__ import annotations

import argparse
import json
import subprocess

from . import control
from .prepare import PROJECT, ROOT
from .scale_acceptance import validate_scale


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
        capture_output=True, text=True, timeout={1002: 360, 10_000: 960, 100_000: 2460}[scale],
    )
    if result.returncode:
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
        [*compose, "up", "-d", "dual-track-monitor"],
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
