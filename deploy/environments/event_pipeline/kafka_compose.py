"""Patch and verify the fixed Dev event-bus Compose runtime guardrails.

The original Dev Kafka topology was provisioned from an external, reviewed
shadow template before the repository had a runtime generator. This tool does
not rebuild that topology. It only removes the obsolete shadow identity and
adds the explicit development identity after proving that the rendered Compose
definition is otherwise equivalent. Kafka's upstream image declares two
ephemeral VOLUME paths in addition to the broker data directory. The fixed
Compose contract mounts those empty runtime paths through broker-specific named
volumes backed by tmpfs.  Explicit named mounts override the image ``VOLUME``
entries, while the local driver options keep their contents memory-only.
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
BROKER_EPHEMERAL_STORAGE = {
    "secrets-tmpfs": {
        "target": "/etc/kafka/secrets",
        "options": "uid=1000,gid=1000,mode=0750,size=1048576",
    },
    "config-tmpfs": {
        "target": "/mnt/shared/config",
        "options": "uid=1000,gid=1000,mode=0750,size=4194304",
    },
}
BROKER_TMPFS = tuple(
    f"{value['target']}:{value['options']}"
    for value in BROKER_EPHEMERAL_STORAGE.values()
)
BROKER_TMPFS_PATHS = tuple(value["target"] for value in BROKER_EPHEMERAL_STORAGE.values())
BROKER_LOGGING = {
    "driver": "json-file",
    "options": {"max-size": "5m", "max-file": "2"},
}
LEGACY_MODEL_SHA256 = "756812ddb694773504874e53d1e76efccd14fc38fe364e7a45ffb3cf6e6aeb28"
HOST_TMPFS_MODEL_SHA256 = "35cbd9a64d8a1d433c09359026e392dba0f37326f3735efea2a01d3e88f81ced"
PREVIOUS_APPROVED_MODEL_SHA256 = "8691774b69e3dd2219d4cc19dd24ce2a0d446636cefcf08d46f4dceda1514278"
APPROVED_MODEL_SHA256 = "a568a7dbd3deb788f8a00a0beca85f68c886366786ae16124ba0b68750b91999"


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


def _broker_volume_name(service: str, suffix: str) -> str:
    if service not in BROKER_SERVICES or suffix not in BROKER_EPHEMERAL_STORAGE:
        raise ValueError("unapproved Kafka ephemeral volume")
    return f"{service}-{suffix}"


def _broker_service_volume_lines(service: str, newline: str) -> list[str]:
    return [
        f"    volumes:{newline}",
        f"      - {service}-data:/var/lib/kafka/data{newline}",
        *[
            f"      - {_broker_volume_name(service, suffix)}:{definition['target']}{newline}"
            for suffix, definition in BROKER_EPHEMERAL_STORAGE.items()
        ],
    ]


def _broker_logging_lines(newline: str) -> list[str]:
    return [
        f"    logging:{newline}",
        f"      driver: json-file{newline}",
        f"      options:{newline}",
        f"        max-size: 5m{newline}",
        f"        max-file: 2{newline}",
    ]


def _root_volume_lines(newline: str) -> list[str]:
    lines = [f"volumes:{newline}"]
    for service in BROKER_SERVICES:
        lines.append(f"  {service}-data: {{}}{newline}")
    for service in BROKER_SERVICES:
        for suffix, definition in BROKER_EPHEMERAL_STORAGE.items():
            lines.extend(
                [
                    f"  {_broker_volume_name(service, suffix)}:{newline}",
                    f"    driver: local{newline}",
                    f"    driver_opts:{newline}",
                    f"      type: tmpfs{newline}",
                    f"      device: tmpfs{newline}",
                    f"      o: {definition['options']}{newline}",
                ]
            )
    return lines


def _replace_root_volumes(lines: list[str], newline: str) -> None:
    roots = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"volumes:\s*", line.rstrip("\r\n"))
    ]
    if len(roots) != 1:
        raise ValueError("Compose must have one root volumes section")
    start = roots[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        body = lines[index].rstrip("\r\n")
        if body and not body.startswith((" ", "#")):
            end = index
            break
    current = lines[start:end]
    legacy = [f"volumes:{newline}"] + [
        f"  {service}-data: {{}}{newline}"
        for service in BROKER_SERVICES
    ]
    expected = _root_volume_lines(newline)
    if current == expected:
        return
    if current != legacy:
        raise ValueError("unexpected Dev event-bus root volumes")
    lines[start:end] = expected


def patch_runtime_guardrails(text: str) -> str:
    """Return Compose text with approved identity and named tmpfs-backed volumes."""
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
    blocks = _service_blocks(lines)
    for service in reversed(BROKER_SERVICES):
        start, end = blocks[service]
        logging_lines = [
            index
            for index in range(start + 1, end)
            if re.fullmatch(r"    logging:\s*", lines[index].rstrip("\r\n"))
        ]
        if len(logging_lines) > 1:
            raise ValueError(f"{service} has duplicate logging blocks")
        expected_logging = _broker_logging_lines(newline)
        if logging_lines:
            logging_start = logging_lines[0]
            logging_end = end
            for index in range(logging_start + 1, end):
                body = lines[index].rstrip("\r\n")
                if body and not body.startswith("      "):
                    logging_end = index
                    break
            if lines[logging_start:logging_end] != expected_logging:
                raise ValueError(f"{service} has unexpected logging configuration")
        else:
            insert_at = next(
                (index for index in range(start + 1, end)
                 if re.match(r"    (?:networks|volumes):", lines[index].rstrip("\r\n"))),
                end,
            )
            lines[insert_at:insert_at] = expected_logging
        blocks = _service_blocks(lines)
        start, end = blocks[service]
        tmpfs_lines = [
            index
            for index in range(start + 1, end)
            if re.fullmatch(r"    tmpfs:\s*", lines[index].rstrip("\r\n"))
        ]
        if len(tmpfs_lines) > 1:
            raise ValueError(f"{service} has duplicate tmpfs blocks")
        expected = [f"      - {value}{newline}" for value in BROKER_TMPFS]
        if tmpfs_lines:
            tmpfs_start = tmpfs_lines[0]
            tmpfs_end = end
            for index in range(tmpfs_start + 1, end):
                body = lines[index].rstrip("\r\n")
                if body and not body.startswith("      "):
                    tmpfs_end = index
                    break
            if lines[tmpfs_start + 1 : tmpfs_end] != expected:
                raise ValueError(f"{service} has unexpected tmpfs mounts")
            del lines[tmpfs_start:tmpfs_end]

        blocks = _service_blocks(lines)
        start, end = blocks[service]
        volume_lines = [
            index
            for index in range(start + 1, end)
            if re.match(r"    volumes:\s*", lines[index].rstrip("\r\n"))
        ]
        if len(volume_lines) != 1:
            raise ValueError(f"{service} must have one volumes declaration")
        volume_start = volume_lines[0]
        body = lines[volume_start].rstrip("\r\n")
        expected_volumes = _broker_service_volume_lines(service, newline)
        if body != "    volumes:":
            if body != f"    volumes: [{service}-data:/var/lib/kafka/data]":
                raise ValueError(f"{service} has unexpected inline volumes")
            lines[volume_start : volume_start + 1] = expected_volumes
            continue
        volume_end = end
        for index in range(volume_start + 1, end):
            child = lines[index].rstrip("\r\n")
            if child and not child.startswith("      "):
                volume_end = index
                break
        if lines[volume_start:volume_end] != expected_volumes:
            raise ValueError(f"{service} has unexpected volumes")

    _replace_root_volumes(lines, newline)
    return "".join(lines)


def patch_identity_labels(text: str) -> str:
    """Compatibility alias for callers of the original identity-only tool."""
    return patch_runtime_guardrails(text)


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


def _named_tmpfs_volume_definition(options: str) -> dict:
    return {
        "driver": "local",
        "driver_opts": {"type": "tmpfs", "device": "tmpfs", "o": options},
    }


def _without_approved_ephemeral_storage(specification: dict) -> dict:
    value = copy.deepcopy(specification)
    root_volumes = value.setdefault("volumes", {})
    for service in BROKER_SERVICES:
        definition = value["services"][service]
        tmpfs = definition.get("tmpfs")
        volumes = definition.get("volumes", [])
        if tmpfs is not None:
            if tuple(tmpfs) != BROKER_TMPFS:
                raise ValueError(f"{service} has unapproved tmpfs configuration")
            definition.pop("tmpfs")
            continue

        expected_targets = {
            item["target"]: _broker_volume_name(service, suffix)
            for suffix, item in BROKER_EPHEMERAL_STORAGE.items()
        }
        retained = []
        found: dict[str, str] = {}
        for mount in volumes:
            target = mount.get("target")
            if target not in expected_targets:
                retained.append(mount)
                continue
            if mount.get("type") != "volume" or mount.get("source") != expected_targets[target]:
                raise ValueError(f"{service} has unapproved ephemeral mount")
            found[target] = mount["source"]
        if found:
            if found != expected_targets:
                raise ValueError(f"{service} has incomplete ephemeral mounts")
            definition["volumes"] = retained
            for suffix, item in BROKER_EPHEMERAL_STORAGE.items():
                name = _broker_volume_name(service, suffix)
                expected = _named_tmpfs_volume_definition(item["options"])
                actual = root_volumes.get(name)
                if actual not in (
                    expected,
                    {**expected, "name": f"{PROJECT}_{name}"},
                ):
                    raise ValueError(f"{service} has unapproved named tmpfs volume")
                root_volumes.pop(name)
    if not root_volumes:
        value.pop("volumes", None)
    return value


def _without_approved_resource_limits(specification: dict) -> dict:
    """Remove only the reviewed broker logging guardrail for baseline comparison."""
    value = copy.deepcopy(specification)
    for service in BROKER_SERVICES:
        logging = value["services"][service].get("logging")
        if logging is not None:
            if logging != BROKER_LOGGING:
                raise ValueError(f"{service} has unapproved logging configuration")
            value["services"][service].pop("logging")
    return value


def _without_approved_tmpfs(specification: dict) -> dict:
    """Compatibility alias retained for callers of the first tmpfs patch."""
    return _without_approved_resource_limits(_without_approved_ephemeral_storage(specification))


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
    proposed = patch_runtime_guardrails(text).encode("utf-8")

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
    if current_model_sha256 not in {
        LEGACY_MODEL_SHA256,
        HOST_TMPFS_MODEL_SHA256,
        PREVIOUS_APPROVED_MODEL_SHA256,
        APPROVED_MODEL_SHA256,
    }:
        raise ValueError("Dev event-bus Compose is not the reviewed baseline")
    if proposed_model_sha256 != APPROVED_MODEL_SHA256:
        raise ValueError("Dev event-bus target does not match the reviewed runtime model")
    if current_model_sha256 != APPROVED_MODEL_SHA256:
        current_without_labels = _without_approved_labels(current_model)
        target_without_labels = _without_approved_labels(proposed_model)
        if _without_approved_resource_limits(
            _without_approved_ephemeral_storage(target_without_labels)
        ) != _without_approved_resource_limits(
            _without_approved_ephemeral_storage(current_without_labels)
        ):
            raise ValueError("Dev event-bus Compose differs beyond approved runtime changes")
    elif _without_approved_labels(current_model) != _without_approved_labels(proposed_model):
        raise ValueError("reviewed Dev event-bus model must be idempotent")

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
    def storage_summary(model: dict, service: str) -> dict:
        targets = set(BROKER_TMPFS_PATHS)
        return {
            "host_tmpfs": model["services"][service].get("tmpfs", []),
            "named_mounts": [
                mount
                for mount in model["services"][service].get("volumes", [])
                if mount.get("target") in targets
            ],
            "named_volume_definitions": {
                name: model.get("volumes", {}).get(name)
                for name in (
                    _broker_volume_name(service, suffix)
                    for suffix in BROKER_EPHEMERAL_STORAGE
                )
                if name in model.get("volumes", {})
            },
        }

    current_storage = {
        service: storage_summary(current_model, service) for service in BROKER_SERVICES
    }
    target_storage = {
        service: storage_summary(proposed_model, service) for service in BROKER_SERVICES
    }
    target_without_storage = _without_approved_ephemeral_storage(proposed_model)
    if any(target_storage[service]["host_tmpfs"] for service in BROKER_SERVICES):
        raise ValueError("Kafka target still uses HostConfig tmpfs")
    if target_without_storage == proposed_model:
        raise ValueError("Kafka target named tmpfs contract is missing")
    return {
        "environment": "development",
        "project": PROJECT,
        "target": str(TARGET),
        "before_sha256": _sha256(original),
        "after_sha256": _sha256(proposed),
        "model_sha256": current_model_sha256,
        "target_model_sha256": proposed_model_sha256,
        "requires_update": original != proposed,
        "current_labels": current_labels,
        "target_labels": proposed_labels,
        "labels_update_required": current_labels != proposed_labels,
        "current_ephemeral_storage": current_storage,
        "target_ephemeral_storage": target_storage,
        "ephemeral_storage_update_required": current_storage != target_storage,
        "tmpfs_update_required": current_storage != target_storage,
        "other_changes": False,
    }


def apply(evidence_id: str) -> dict:
    before = measure()
    if not re.fullmatch(r"dev-eventbus-(?:label|runtime)-[0-9a-f]{16}", evidence_id or ""):
        raise ValueError("fresh Dev event-bus runtime evidence ID required")
    evidence = EVIDENCE_ROOT / evidence_id
    if evidence.exists() or evidence.is_symlink():
        raise ValueError("evidence directory already exists")
    evidence.mkdir(mode=0o700)

    original = TARGET.read_bytes()
    backup = evidence / "compose.before.yml"
    backup.write_bytes(original)
    backup.chmod(0o600)
    proposed = patch_runtime_guardrails(original.decode("utf-8")).encode("utf-8")
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
        "change_scope": "identity_labels_and_kafka_named_tmpfs_volumes",
        "environment": "development",
        "project": PROJECT,
        "evidence_id": evidence_id,
        "before_sha256": before["before_sha256"],
        "after_sha256": after["before_sha256"],
        "labels_updated": before["labels_update_required"],
        "tmpfs_updated": before["tmpfs_update_required"],
        "named_tmpfs_volumes_updated": before["ephemeral_storage_update_required"],
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
        ephemeral_mounts = {
            mount.get("Destination"): mount
            for mount in item.get("Mounts", [])
            if mount.get("Destination") in BROKER_TMPFS_PATHS
        }
        anonymous_volumes = [
            mount
            for mount in item.get("Mounts", [])
            if mount.get("Type") == "volume"
            and re.fullmatch(r"[0-9a-f]{64}", mount.get("Name") or "")
        ]
        expected_volume = f"{PROJECT}_{service}-data"
        expected_ephemeral_volumes = {
            storage["target"]: f"{PROJECT}_{_broker_volume_name(service, suffix)}"
            for suffix, storage in BROKER_EPHEMERAL_STORAGE.items()
        }
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
            or set(ephemeral_mounts) != set(BROKER_TMPFS_PATHS)
            or any(
                mount.get("Type") != "volume"
                or mount.get("Name") != expected_ephemeral_volumes[path]
                or not mount.get("RW")
                for path, mount in ephemeral_mounts.items()
            )
            or anonymous_volumes
        ):
            raise ValueError(f"{service} runtime identity mismatch")
        logging = (item.get("HostConfig", {}).get("LogConfig") or {})
        if logging.get("Type") != BROKER_LOGGING["driver"] or logging.get("Config") != BROKER_LOGGING["options"]:
            raise ValueError(f"{service} log rotation contract mismatch")
        filesystem_types = _checked(
            ["docker", "exec", name, "stat", "-f", "-c", "%T", *BROKER_TMPFS_PATHS]
        ).splitlines()
        permissions = _checked(
            ["docker", "exec", name, "stat", "-c", "%u:%g:%a", *BROKER_TMPFS_PATHS]
        ).splitlines()
        if filesystem_types != ["tmpfs"] * len(BROKER_TMPFS_PATHS) or permissions != [
            "1000:1000:750"
        ] * len(BROKER_TMPFS_PATHS):
            raise ValueError(f"{service} tmpfs filesystem contract mismatch")
        verified[service] = {
            "container_id": item.get("Id"),
            "image_id": item.get("Image"),
            "volume": expected_volume,
            "network": NETWORK,
            "tmpfs_backed_volumes": expected_ephemeral_volumes,
            "logging": BROKER_LOGGING,
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
        for suffix, storage in BROKER_EPHEMERAL_STORAGE.items():
            tmpfs_name = f"{PROJECT}_{_broker_volume_name(service, suffix)}"
            tmpfs_volume = json.loads(
                _checked(["docker", "volume", "inspect", tmpfs_name])
            )[0]
            if (
                tmpfs_volume.get("Driver") != "local"
                or (tmpfs_volume.get("Labels") or {}).get("com.docker.compose.project")
                != PROJECT
                or tmpfs_volume.get("Options")
                != {
                    "type": "tmpfs",
                    "device": "tmpfs",
                    "o": storage["options"],
                }
            ):
                raise ValueError(f"{service} named tmpfs volume identity mismatch")
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
