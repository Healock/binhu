#!/usr/bin/env python3
"""Remove only stale, empty Staging event-pipeline networks.

This is deliberately narrower than ``docker network prune``.  Networks with
any attached container, including FRP, are never eligible for removal.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess


STATE = Path("/var/lib/binhu-staging-event-pipeline")
EVIDENCE = STATE / "evidence"
RUN_NETWORK_RE = re.compile(
    r"^binhu-staging-event-pipeline-STG-[0-9]{8}-[0-9]{2}_internal$"
)
RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
PROTECTED_RUN = "STG-20261002-27"
PROTECTED_NAME_PARTS = (
    "binhu-staging_internal",
    "binhu-staging-pipeline-backend",
    "binhu-staging-backend",
    "binhu-production",
    "frp",
)


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60).stdout


def _inspect(network_id: str) -> dict:
    payload = json.loads(_run("docker", "network", "inspect", network_id))
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("network_inspect_invalid")
    return payload[0]


def _protected(name: str, info: dict) -> str | None:
    lowered = name.lower()
    for part in PROTECTED_NAME_PARTS:
        if part in lowered:
            return "protected_name"
    labels = info.get("Labels") or {}
    if str(labels.get("binhu.environment", "")).lower() in {"production", "prod"}:
        return "production_label"
    if str(labels.get("binhu.role", "")).lower() in {"frp", "tunnel", "edge-tunnel"}:
        return "frp_label"
    containers = info.get("Containers") or {}
    for container in containers.values():
        container_name = str((container or {}).get("Name", "")).lower()
        if "frp" in container_name:
            return "frp_container"
        if "production" in container_name:
            return "production_container"
    return None


def _eligible(name: str, info: dict) -> str | None:
    if name in {"bridge", "host", "none"}:
        return "builtin"
    if info.get("Driver") != "bridge":
        return "non_bridge"
    if info.get("Containers"):
        return "attached_containers"
    if (reason := _protected(name, info)):
        return reason
    if not RUN_NETWORK_RE.fullmatch(name):
        return "not_staging_event_pipeline"
    return None


def _cleanup_stopped_test_containers() -> list[dict]:
    records: list[dict] = []
    ids = [line.strip() for line in _run("docker", "ps", "-aq", "--filter", "status=exited").splitlines() if line.strip()]
    for container_id in ids:
        inspected = json.loads(_run("docker", "inspect", container_id))[0]
        config = inspected.get("Config") or {}
        labels = config.get("Labels") or {}
        name = str(inspected.get("Name", "")).lstrip("/")
        run_id = str(labels.get("binhu.run_id", ""))
        record = {"id_prefix": container_id[:12], "name": name, "run_id": run_id, "action": "skipped"}
        if (labels.get("binhu.environment") != "staging"
                or labels.get("binhu.production_data") != "false"):
            record["reason"] = "identity_not_staging_test"
        elif run_id == PROTECTED_RUN:
            record["reason"] = "current_run_protected"
        elif not RUN_RE.fullmatch(run_id):
            record["reason"] = "run_id_not_fixed"
        elif "frp" in name.lower():
            record["reason"] = "frp_container"
        else:
            _run("docker", "rm", container_id)
            record["action"] = "removed"
            record["reason"] = "stopped_staging_test_container"
        records.append(record)
    return records


def cleanup() -> dict:
    container_records = _cleanup_stopped_test_containers()
    ids = [line.strip() for line in _run("docker", "network", "ls", "--filter", "type=custom", "-q").splitlines() if line.strip()]
    records: list[dict] = []
    for network_id in ids:
        info = _inspect(network_id)
        name = str(info.get("Name", ""))
        reason = _eligible(name, info)
        record = {
            "id_prefix": network_id[:12],
            "name": name,
            "driver": info.get("Driver"),
            "internal": bool(info.get("Internal")),
            "container_count": len(info.get("Containers") or {}),
            "action": "skipped" if reason else "remove",
            "reason": reason,
        }
        if reason is None:
            try:
                _run("docker", "network", "rm", network_id)
                record["action"] = "removed"
            except subprocess.CalledProcessError:
                record["action"] = "remove_failed"
                record["reason"] = "docker_network_rm_failed"
        records.append(record)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = EVIDENCE / f"network-cleanup-{stamp}.json"
    payload = {
        "environment": "staging",
        "operation": "cleanup-unused-networks",
        "production_data": False,
        "containers": container_records,
        "records": records,
        "removed_container_count": sum(record["action"] == "removed" for record in container_records),
        "removed_count": sum(record["action"] == "removed" for record in records),
        "skipped_count": sum(record["action"] == "skipped" for record in records),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(json.dumps({"environment": "staging", "operation": payload["operation"], "evidence": str(path),
                      "removed_container_count": payload["removed_container_count"],
                      "removed_count": payload["removed_count"], "skipped_count": payload["skipped_count"]}, sort_keys=True))
    return payload


if __name__ == "__main__":
    import os
    if os.geteuid() != 0:
        raise SystemExit("root execution required")
    cleanup()
