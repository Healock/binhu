#!/usr/bin/env python3
"""Install the fixed internal Staging database access network without restarts.

Called by the admin installation workflow, never through the relay SSH account.
Only the two live Staging database containers may be connected. Their existing
application network and all volumes/configuration remain untouched.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

NETWORK = "binhu-staging-pipeline-backend"
APP_NETWORK = "binhu-staging_internal"
LABELS = {"binhu.environment": "staging", "binhu.role": "pipeline-backend-access"}
SERVICES = {
    "binhu-staging-environment-mysql-1": ("environment-mysql", ("environment-mysql",)),
    "binhu-staging-redis-1": ("redis", ("redis", "environment-redis")),
}
EVIDENCE = Path("/srv/deploy-backups/environment-triad/staging-network")


def run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if result.returncode:
        # Never expose Docker errors/inspect Env or arbitrary driver output.
        raise ValueError("staging_network_command_failed")
    return result.stdout


def inspect(kind: str, name: str) -> dict:
    return json.loads(run(["docker", kind, "inspect", name]))[0]


def network_if_present() -> dict | None:
    ids = run(["docker", "network", "ls", "-q", "--filter", f"name=^{NETWORK}$"]).split()
    if not ids:
        return None
    if len(ids) != 1:
        raise ValueError("staging_access_network_not_unique")
    return inspect("network", NETWORK)


def validate_network(network: dict) -> None:
    if (network.get("Name") != NETWORK or network.get("Driver") != "bridge"
            or network.get("Internal") is not True
            or any((network.get("Labels") or {}).get(k) != v for k, v in LABELS.items())):
        raise ValueError("staging_access_network_identity_mismatch")
    for item in (network.get("Containers") or {}).values():
        name = item.get("Name", "")
        if name not in SERVICES and not re.fullmatch(
                r"binhu-staging-event-pipeline-stg-[0-9]{8}-[0-9]{2}-(business-bridge|backend-outbox-relay)-1", name):
            raise ValueError("staging_access_network_foreign_member")
        container = inspect("container", name)
        labels = container.get("Config", {}).get("Labels", {}) or {}
        if labels.get("binhu.environment") != "staging":
            raise ValueError("staging_access_network_foreign_member")
        if name not in SERVICES:
            expected = name.rsplit("-", 3)[0]  # validate by exact compose name below
            project = labels.get("com.docker.compose.project", "")
            if (not re.fullmatch(r"binhu-staging-event-pipeline-stg-[0-9]{8}-[0-9]{2}", project)
                    or name not in {project + "-business-bridge-1", project + "-backend-outbox-relay-1"}):
                raise ValueError("staging_access_network_foreign_member")


def service_state() -> dict:
    state = {}
    app = inspect("network", APP_NETWORK)
    if ((app.get("Labels") or {}).get("com.docker.compose.project") != "binhu-staging"
            or (app.get("Labels") or {}).get("binhu.environment") != "staging"):
        raise ValueError("staging_application_network_identity_mismatch")
    for name, (service, _) in SERVICES.items():
        item = inspect("container", name)
        labels = item.get("Config", {}).get("Labels", {}) or {}
        networks = item.get("NetworkSettings", {}).get("Networks", {}) or {}
        if (item.get("Name") != "/" + name or item.get("State", {}).get("Status") != "running"
                or item.get("State", {}).get("OOMKilled") is True
                or labels.get("binhu.environment") != "staging"
                or labels.get("com.docker.compose.project") != "binhu-staging"
                or labels.get("com.docker.compose.service") != service
                or APP_NETWORK not in networks or set(networks) - {APP_NETWORK, NETWORK}):
            raise ValueError("staging_database_container_identity_mismatch")
        state[name] = {
            "id": item["Id"], "image": item["Image"],
            "started_at": item["State"].get("StartedAt"), "restart_count": item.get("RestartCount"),
            "networks": sorted(networks), "labels": {k: labels[k] for k in (
                "binhu.environment", "com.docker.compose.project", "com.docker.compose.service")},
            "access_aliases": (networks.get(NETWORK) or {}).get("Aliases") or [],
        }
    return {"application_network": public_network(app), "services": state}


def public_network(network: dict) -> dict:
    return {"name": network.get("Name"), "id": network.get("Id"), "internal": network.get("Internal"),
            "driver": network.get("Driver"), "labels": network.get("Labels") or {},
            "members": sorted(x.get("Name", "") for x in (network.get("Containers") or {}).values())}


def health() -> dict:
    results = {}
    for name in ("bootstrap", "health"):
        path = "/api/app/bootstrap" if name == "bootstrap" else "/api/health"
        with urllib.request.urlopen("http://127.0.0.1:48126" + path, timeout=10) as response:
            if response.status != 200:
                raise ValueError("staging_health_probe_failed")
            data = json.loads(response.read(65536))
        if name == "bootstrap":
            if data.get("environment") != "staging" or data.get("api_entry") != "/staging/api":
                raise ValueError("staging_bootstrap_identity_mismatch")
            results[name] = {k: data.get(k) for k in ("environment", "api_entry", "server_version")}
        else:
            results[name] = {"status_code": 200, "version": data.get("version")}
    return results


def stable(before: dict, after: dict) -> None:
    if before["application_network"] != after["application_network"]:
        raise ValueError("staging_application_network_changed")
    for name in SERVICES:
        if any(before["services"][name][key] != after["services"][name][key]
               for key in ("id", "image", "started_at", "restart_count", "labels")):
            raise ValueError("staging_database_service_changed")


def verify() -> dict:
    state = service_state()
    network = network_if_present()
    if network is None:
        raise ValueError("staging_access_network_missing")
    validate_network(network)
    for name, (_, aliases) in SERVICES.items():
        if (NETWORK not in state["services"][name]["networks"]
                or not set(aliases) <= set(state["services"][name]["access_aliases"])):
            raise ValueError("staging_access_endpoint_missing")
    return {"environment": "staging", "verified": True, "network": public_network(network),
            "state": state, "health": health()}


def save(directory: Path, name: str, value: dict) -> None:
    path = directory / name
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    path.chmod(0o600)


def apply() -> dict:
    before = service_state()
    baseline_health = health()
    network = network_if_present()
    if network is not None:
        validate_network(network)
    if EVIDENCE.is_symlink() or EVIDENCE.parent.is_symlink():
        raise ValueError("staging_network_evidence_path_invalid")
    EVIDENCE.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="network-", dir=EVIDENCE))
    save(directory, "before.json", {**before, "health": baseline_health,
         "access_network": public_network(network) if network else None})
    added = []
    created = network is None
    try:
        if created:
            run(["docker", "network", "create", "--internal", "--driver", "bridge",
                 "--label", "binhu.environment=staging", "--label", "binhu.role=pipeline-backend-access", NETWORK])
        for name, (_, aliases) in SERVICES.items():
            if NETWORK not in before["services"][name]["networks"]:
                command = ["docker", "network", "connect"]
                for alias in aliases:
                    command += ["--alias", alias]
                run([*command, NETWORK, name])
                added.append(name)
        result = verify()
        stable(before, result["state"])
        if baseline_health != result["health"]:
            raise ValueError("staging_application_health_changed")
        result.update({"applied": True, "time_utc": datetime.now(timezone.utc).isoformat(),
                       "evidence": str(directory), "new_connections": added})
        save(directory, "result.json", result)
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.glob("*.json")}
        save(directory, "sha256.json", hashes)
        return result
    except Exception:
        rollback = True
        for name in reversed(added):
            try:
                run(["docker", "network", "disconnect", NETWORK, name])
            except Exception:
                rollback = False
        if created:
            try:
                run(["docker", "network", "rm", NETWORK])
            except Exception:
                rollback = False
        save(directory, "failure.json", {"error_code": "staging_access_setup_failed", "rollback": rollback})
        raise ValueError("staging_access_setup_failed_" + ("rolled_back" if rollback else "rollback_failed")) from None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("measure", "apply"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("root_required")
    try:
        result = apply() if args.action == "apply" else verify()
        print(json.dumps(result, sort_keys=True))
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r"staging_[a-z_]+", str(error)) else "staging_access_setup_error"
        raise SystemExit(code) from None


if __name__ == "__main__":
    main()
