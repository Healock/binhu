"""Patch and verify the fixed Dev event-bus Compose identity labels.

The original Dev Kafka topology was provisioned from an external, reviewed
shadow template before the repository had a runtime generator. This tool does
not rebuild that topology. It only removes the obsolete shadow identity and
adds the explicit development identity after proving that the rendered Compose
definition is otherwise equivalent.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


PROJECT = "binhu-development-eventbus"
ROOT = Path("/srv/binhu-environments/development-eventbus")
TARGET = ROOT / "compose.yml"
EVIDENCE_ROOT = Path("/srv/deploy-backups/environment-triad")
NETWORK = PROJECT + "_internal"
BASE_SERVICES = ("kafka-1", "kafka-2", "kafka-3", "schema-registry")
BROKER_SERVICES = BASE_SERVICES[:3]
APPROVED_LABELS = ("binhu.environment", "binhu.shadow")
APPROVED_MODEL_SHA256 = "756812ddb694773504874e53d1e76efccd14fc38fe364e7a45ffb3cf6e6aeb28"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _service_blocks(lines: list[str]) -> dict[str, tuple[int, int]]:
    services_line = next(
        (index for index, line in enumerate(lines) if re.fullmatch(r"services:\s*", line.rstrip("\r\n"))),
        None,
    )
    if services_line is None:
        raise ValueError("Compose services section is missing")
    starts: list[tuple[str, int]] = []
    for index in range(services_line + 1, len(lines)):
        body = lines[index].rstrip("\r\n")
        if body and not body.startswith((" ", "#")):
            break
        match = re.fullmatch(r"  ([A-Za-z0-9._-]+):\s*", body)
        if match:
            starts.append((match.group(1), index))
    blocks: dict[str, tuple[int, int]] = {}
    for position, (name, start) in enumerate(starts):
        end = starts[position + 1][1] if position + 1 < len(starts) else len(lines)
        for index in range(start + 1, end):
            body = lines[index].rstrip("\r\n")
            if body and not body.startswith((" ", "#")):
                end = index
                break
        blocks[name] = (start, end)
    return blocks


def patch_identity_labels(text: str) -> str:
    """Return the same Compose text with only the approved identity changes."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    blocks = _service_blocks(lines)
    if set(blocks) != set(BASE_SERVICES):
        raise ValueError("unexpected Dev event-bus service set")

    for service in reversed(BASE_SERVICES):
        start, end = blocks[service]
        labels = [
            index
            for index in range(start + 1, end)
            if re.fullmatch(r"    labels:\s*", lines[index].rstrip("\r\n"))
        ]
        if len(labels) != 1:
            raise ValueError(f"{service} must have one mapping labels block")
        label_start = labels[0]
        label_end = end
        for index in range(label_start + 1, end):
            body = lines[index].rstrip("\r\n")
            if body and not body.startswith("      "):
                label_end = index
                break

        environment_lines = []
        shadow_lines = []
        for index in range(label_start + 1, label_end):
            body = lines[index].rstrip("\r\n")
            if re.fullmatch(r"      binhu\.environment:\s*development\s*", body):
                environment_lines.append(index)
            elif re.match(r"      binhu\.environment:\s*", body):
                raise ValueError(f"{service} has a non-development environment label")
            if re.match(r"      binhu\.shadow:\s*", body):
                shadow_lines.append(index)
        if len(environment_lines) > 1 or len(shadow_lines) > 1:
            raise ValueError(f"{service} has duplicate identity labels")

        for index in reversed(shadow_lines):
            del lines[index]
        if not environment_lines:
            lines.insert(label_start + 1, f"      binhu.environment: development{newline}")
    return "".join(lines)


def _checked(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("Dev event-bus inspection failed")
    return result.stdout


def _render(path: Path) -> dict:
    return json.loads(
        _checked(
            ["docker", "compose", "-f", str(path), "config", "--format", "json"],
            cwd=ROOT,
        )
    )


def _without_approved_labels(specification: dict) -> dict:
    value = copy.deepcopy(specification)
    if value.get("name") != PROJECT or set(value.get("services", {})) != set(BASE_SERVICES):
        raise ValueError("unexpected Dev event-bus project or services")
    for service in BASE_SERVICES:
        labels = value["services"][service].setdefault("labels", {})
        for label in APPROVED_LABELS:
            labels.pop(label, None)
    return value


def _model_sha256(specification: dict) -> str:
    payload = json.dumps(
        _without_approved_labels(specification),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(payload)


def _fixed_target() -> None:
    if ROOT.resolve() != ROOT or ROOT.is_symlink() or ROOT.parent.is_symlink():
        raise ValueError("fixed Dev event-bus root required")
    if TARGET.parent != ROOT or TARGET.is_symlink() or not TARGET.is_file():
        raise ValueError("fixed Dev event-bus Compose file required")


def measure() -> dict:
    _fixed_target()
    original = TARGET.read_bytes()
    text = original.decode("utf-8")
    proposed = patch_identity_labels(text).encode("utf-8")

    with tempfile.NamedTemporaryFile(
        dir=ROOT, prefix=".eventbus-label-", suffix=".yml", delete=False
    ) as handle:
        handle.write(proposed)
        temporary = Path(handle.name)
    try:
        temporary.chmod(0o600)
        current_model = _render(TARGET)
        proposed_model = _render(temporary)
    finally:
        temporary.unlink(missing_ok=True)

    current_model_sha256 = _model_sha256(current_model)
    proposed_model_sha256 = _model_sha256(proposed_model)
    if current_model_sha256 != APPROVED_MODEL_SHA256:
        raise ValueError("Dev event-bus Compose is not the reviewed baseline")
    if proposed_model_sha256 != current_model_sha256:
        raise ValueError("Dev event-bus Compose differs beyond approved identity labels")

    current_labels = {
        service: current_model["services"][service].get("labels", {})
        for service in BASE_SERVICES
    }
    proposed_labels = {
        service: proposed_model["services"][service].get("labels", {})
        for service in BASE_SERVICES
    }
    for service, labels in proposed_labels.items():
        if labels.get("binhu.environment") != "development" or "binhu.shadow" in labels:
            raise ValueError(f"{service} target identity is invalid")
    return {
        "environment": "development",
        "project": PROJECT,
        "target": str(TARGET),
        "before_sha256": _sha256(original),
        "after_sha256": _sha256(proposed),
        "model_sha256": current_model_sha256,
        "requires_update": original != proposed,
        "current_labels": current_labels,
        "target_labels": proposed_labels,
        "other_changes": False,
    }


def apply(evidence_id: str) -> dict:
    before = measure()
    if not re.fullmatch(r"dev-eventbus-label-[0-9a-f]{16}", evidence_id or ""):
        raise ValueError("fresh Dev event-bus label evidence ID required")
    evidence = EVIDENCE_ROOT / evidence_id
    if evidence.exists() or evidence.is_symlink():
        raise ValueError("evidence directory already exists")
    evidence.mkdir(mode=0o700)

    original = TARGET.read_bytes()
    backup = evidence / "compose.before.yml"
    backup.write_bytes(original)
    backup.chmod(0o600)
    proposed = patch_identity_labels(original.decode("utf-8")).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".eventbus-compose-", delete=False) as handle:
        handle.write(proposed)
        temporary = Path(handle.name)
    try:
        temporary.chmod(0o600)
        os.replace(temporary, TARGET)
    finally:
        temporary.unlink(missing_ok=True)

    after = measure()
    report = {
        "environment": "development",
        "project": PROJECT,
        "evidence_id": evidence_id,
        "before_sha256": before["before_sha256"],
        "after_sha256": after["before_sha256"],
        "labels_updated": before["requires_update"],
        "other_changes": after["other_changes"],
        "definition_verified": not after["requires_update"],
        "runtime_verified": False,
    }
    result = evidence / "compose-update.json"
    result.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    result.chmod(0o600)
    return report


def verify_runtime() -> dict:
    definition = measure()
    if definition["requires_update"]:
        raise ValueError("Dev event-bus Compose identity update is incomplete")
    names = [f"{PROJECT}-{service}-1" for service in BROKER_SERVICES]
    containers = json.loads(_checked(["docker", "inspect", *names]))
    verified: dict[str, dict] = {}
    for service, name, item in zip(BROKER_SERVICES, names, containers, strict=True):
        labels = item.get("Config", {}).get("Labels") or {}
        mounts = [
            mount
            for mount in item.get("Mounts", [])
            if mount.get("Destination") == "/var/lib/kafka/data"
        ]
        expected_volume = f"{PROJECT}_{service}-data"
        if (
            item.get("Name", "").lstrip("/") != name
            or not item.get("State", {}).get("Running")
            or labels.get("com.docker.compose.project") != PROJECT
            or labels.get("com.docker.compose.service") != service
            or labels.get("binhu.environment") != "development"
            or "binhu.shadow" in labels
            or set((item.get("NetworkSettings", {}).get("Networks") or {})) != {NETWORK}
            or len(mounts) != 1
            or mounts[0].get("Name") != expected_volume
            or not mounts[0].get("RW")
        ):
            raise ValueError(f"{service} runtime identity mismatch")
        verified[service] = {
            "container_id": item.get("Id"),
            "image_id": item.get("Image"),
            "volume": expected_volume,
            "network": NETWORK,
        }

    network = json.loads(_checked(["docker", "network", "inspect", NETWORK]))[0]
    if (
        not network.get("Internal")
        or (network.get("Labels") or {}).get("com.docker.compose.project") != PROJECT
    ):
        raise ValueError("Dev event-bus network identity mismatch")
    for service in BROKER_SERVICES:
        volume_name = f"{PROJECT}_{service}-data"
        volume = json.loads(_checked(["docker", "volume", "inspect", volume_name]))[0]
        if (volume.get("Labels") or {}).get("com.docker.compose.project") != PROJECT:
            raise ValueError(f"{service} data volume identity mismatch")
    return {
        "environment": "development",
        "project": PROJECT,
        "verified": True,
        "definition_sha256": definition["before_sha256"],
        "network_id": network.get("Id"),
        "brokers": verified,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("measure", "apply", "verify"))
    parser.add_argument("--evidence-id")
    args = parser.parse_args()
    try:
        if args.action == "apply":
            result = apply(args.evidence_id or "")
        elif args.action == "verify":
            result = verify_runtime()
        else:
            result = measure()
        print(json.dumps(result))
    except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError, subprocess.SubprocessError):
        raise SystemExit(
            "Dev event-bus identity update refused; preserve current brokers, volumes, and network"
        ) from None


if __name__ == "__main__":
    main()
