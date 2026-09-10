"""Measured, hash-fenced startup of the explicitly prepared Dev-only project."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

from .prepare import ROOT, PROJECT, NETWORK, checked, compose


def measure():
    if ROOT.is_symlink() or ROOT.parent.is_symlink() or ROOT.resolve() != ROOT:
        raise ValueError("unexpected Dev root")
    manifest = json.loads((ROOT / "manifest.json").read_text())
    if manifest.get("environment") != "development" or manifest.get("project") != PROJECT:
        raise ValueError("manifest identity mismatch")
    for name, digest in manifest["hashes"].items():
        path = ROOT / name
        if path.is_symlink() or path.parent != ROOT or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("runtime file identity mismatch")
    spec = json.loads((ROOT / "compose.json").read_text())
    if spec != compose(manifest["images"]):
        raise ValueError("runtime differs from closed Dev resource definition")
    network = json.loads(checked(["docker", "network", "inspect", NETWORK]))[0]
    if not network.get("Internal") or network.get("Labels", {}).get("com.docker.compose.project") != "binhu-development-eventbus":
        raise ValueError("Dev event network identity mismatch")
    for info in network.get("Containers", {}).values():
        if not info["Name"].startswith("binhu-development-"):
            raise ValueError("foreign network dependency")
    all_ids = checked(["docker", "ps", "-aq"]).split()
    containers = json.loads(checked(["docker", "inspect", *all_ids])) if all_ids else []
    for item in containers:
        project = item["Config"].get("Labels", {}).get("com.docker.compose.project")
        for mount in item.get("Mounts", []):
            if mount.get("Name", "").startswith(PROJECT + "_") and project != PROJECT:
                raise ValueError("Dev volume referenced by another project")
        if project == PROJECT:
            if set(item["NetworkSettings"]["Networks"]) != {NETWORK}:
                raise ValueError("pipeline container has unexpected network")
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(memory["MemAvailable"].split()[0])
    if available < 2 * 1024**2:
        raise ValueError("insufficient memory reserve")
    checked(["docker", "compose", "-f", str(ROOT / "compose.json"), "config", "--quiet"])
    return {"environment": "development", "project": PROJECT, "run_id": manifest["run_id"],
            "hashes_verified": True, "isolation_verified": True, "memory_available_kib": available}


def apply():
    report = measure()
    evidence = ROOT / ("apply-evidence-" + str(time.time_ns()))
    # Never overwrite earlier success or failure output.
    evidence.mkdir(mode=0o700)
    schema = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.json"),
                             "run", "--rm", "--no-deps", "relay", "python", "-m",
                             "event_pipeline.schema_registry", "verify"],
                            capture_output=True, text=True, timeout=45)
    path = evidence / "schema-check.log"
    path.write_text(schema.stdout + "\n" + schema.stderr)
    path.chmod(0o600)
    if schema.returncode:
        raise ValueError("register the fixed Dev schema before starting workers")
    try:
        result = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.json"),
                                 "up", "-d"], capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        path = evidence / "startup-timeout.json"
        path.write_text(json.dumps({"startup_timeout": True, "acceptance": "pending"}))
        path.chmod(0o600)
        raise ValueError("startup deadline reached; preserve resources and remeasure") from None
    path = evidence / "startup.log"
    path.write_text(result.stdout + "\n" + result.stderr)
    path.chmod(0o600)
    if result.returncode:
        raise ValueError("startup failed; preserve private diagnostics")
    return {**report, "startup_requested": True, "acceptance": "pending"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("measure", "apply"))
    args = parser.parse_args()
    try:
        print(json.dumps(apply() if args.mode == "apply" else measure()))
    except Exception:
        raise SystemExit("Dev pipeline control refused; inspect private evidence") from None
