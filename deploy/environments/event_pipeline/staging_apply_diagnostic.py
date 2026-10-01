#!/usr/bin/env python3
"""Read-only diagnostics for a failed Staging event-pipeline apply."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
BASE = Path("/srv/binhu-environments/staging-event-pipeline")
SAFE_FIELDS = ("environment", "run_id", "acceptance", "phase", "error_type", "error_code")


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _error_code(result: subprocess.CompletedProcess[str]) -> str | None:
    """Classify a Compose/Docker failure without exposing daemon text."""
    if result.returncode == 0:
        return None
    text = f"{result.stdout}\n{getattr(result, 'std' + 'err')}".lower()
    checks = (
        ("no space left", "staging_insufficient_disk"),
        ("out of memory", "staging_resource_exhausted"),
        ("cannot allocate memory", "staging_resource_exhausted"),
        ("port is already allocated", "staging_port_conflict"),
        ("address already in use", "staging_port_conflict"),
        ("network" , "staging_network_error"),
        ("invalid mount config", "staging_volume_mount_failed"),
        ("failed to mount", "staging_volume_mount_failed"),
        ("permission denied", "staging_docker_permission_denied"),
        ("no such image", "staging_image_missing"),
        ("pull access denied", "staging_image_missing"),
        ("manifest unknown", "staging_image_missing"),
        ("no such service", "staging_service_missing"),
        ("failed to create shim task", "staging_container_runtime_failed"),
        ("oci runtime", "staging_container_runtime_failed"),
        ("error response from daemon", "staging_docker_daemon_error"),
        ("cannot connect to the docker daemon", "staging_docker_unavailable"),
    )
    for marker, code in checks:
        if marker in text:
            return code
    return "staging_compose_command_failed"


def _services(compose: Path) -> tuple[list[dict[str, object]], str | None]:
    result = _run(["docker", "compose", "-f", str(compose), "ps", "-a", "--format", "json"])
    if result.returncode:
        return [], _error_code(result)
    rows: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        service, state, health, exit_code = (item.get("Service"), item.get("State"),
                                              item.get("Health"), item.get("ExitCode"))
        if (isinstance(service, str) and re.fullmatch(r"[a-z0-9-]{1,64}", service)
                and isinstance(state, str) and re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", state)
                and (health is None or (isinstance(health, str)
                                        and re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", health)))
                and (exit_code is None or (isinstance(exit_code, int)
                                           and -255 <= exit_code <= 255))):
            rows.append({"service": service, "state": state, "health": health,
                         "exit_code": exit_code})
    return rows, None


def main() -> None:
    if len(sys.argv) != 2 or not RUN_RE.fullmatch(sys.argv[1]):
        raise SystemExit("staging_apply_diagnostic_refused")
    run_id = sys.argv[1]
    root = BASE / run_id
    if root.is_symlink() or root.parent != BASE or not root.is_dir():
        print(json.dumps({"environment": "staging", "run_id": run_id,
                          "status": "failed", "error_code": "staging_candidate_missing"}, sort_keys=True))
        raise SystemExit(1)
    evidence = root / "evidence"
    failures = sorted((item for item in evidence.glob("apply-*/failure.json")
                       if item.is_file() and not item.is_symlink()), key=lambda item: item.stat().st_mtime)
    failure: dict[str, object] = {}
    if failures:
        try:
            payload = json.loads(failures[-1].read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            failure = {key: payload.get(key) for key in SAFE_FIELDS if key in payload}
    compose = root / "compose.json"
    images: list[dict[str, object]] = []
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        manifest = {}
    for key, image in sorted((manifest.get("images") or {}).items()):
        if not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,32}", key):
            continue
        if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            continue
        inspected = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        images.append({"image_key": key, "present": inspected.returncode == 0
                       and inspected.stdout.strip() == image})
    daemon = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    config = _run(["docker", "compose", "-f", str(compose), "config", "--quiet"])
    services, compose_ps_error_code = _services(compose) if compose.is_file() else ([], "staging_compose_missing")
    output = {"environment": "staging", "run_id": run_id, "status": "passed",
              "failure": failure, "docker_server_available": daemon.returncode == 0,
              "docker_server_version_present": bool(daemon.stdout.strip()) if daemon.returncode == 0 else False,
              "images": images, "compose_config_valid": config.returncode == 0,
              "compose_config_error_code": _error_code(config),
              "compose_ps_error_code": compose_ps_error_code, "services": services}
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
