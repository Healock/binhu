"""Generate and safely patch the fixed Dev Flink Compose definition."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile


PROJECT = "binhu-development-flink"
ROOT = Path("/srv/binhu-environments/development-eventbus")
TARGET = ROOT / "flink-pipeline-compose.json"
EVIDENCE_ROOT = Path("/srv/deploy-backups/environment-triad")
NETWORK = "binhu-development-eventbus_internal"
CHECKPOINT_VOLUME = "binhu-development_flink-checkpoints"
PIPELINE_SQL = PurePosixPath("/srv/binhu-environments/development-pipeline/pipeline.sql")
ARTIFACT_NAMES = (
    "flink-sql-connector-kafka-3.3.0-1.20.jar",
    "flink-connector-jdbc-3.3.0-1.20.jar",
    "mysql-connector-j-8.4.0.jar",
    "dev-pipeline-job.jar",
)
FLINK_PROPERTIES = """jobmanager.memory.process.size: 640m
jobmanager.memory.jvm-metaspace.size: 128m
jobmanager.memory.jvm-overhead.min: 64m
jobmanager.memory.jvm-overhead.max: 64m
taskmanager.memory.process.size: 640m
taskmanager.memory.jvm-metaspace.size: 128m
taskmanager.memory.jvm-overhead.min: 64m
taskmanager.memory.jvm-overhead.max: 64m
taskmanager.memory.network.min: 32m
taskmanager.memory.network.max: 32m
taskmanager.memory.managed.size: 64m
taskmanager.numberOfTaskSlots: 1
"""


def _image(value: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value or ""):
        raise ValueError("immutable Flink image required")
    return value


def _artifact_root(value: Path) -> PurePosixPath:
    value = PurePosixPath(str(value).replace("\\", "/"))
    if not value.is_absolute():
        raise ValueError("absolute Dev artifact directory required")
    if value.parent != PurePosixPath("/srv/binhu-environments") or not re.fullmatch(
        r"build-dev-pipeline-[0-9a-f]{8,16}", value.name
    ):
        raise ValueError("unexpected Dev artifact directory")
    return value


def _mount(source: Path, target: str) -> dict:
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": True,
    }


def specification(image: str, artifact_root: Path) -> dict:
    image = _image(image)
    artifact_root = _artifact_root(artifact_root)
    mounts = [
        {
            "type": "volume",
            "source": "flink-checkpoints",
            "target": "/opt/flink/checkpoints",
            "volume": {},
        },
        _mount(artifact_root / ARTIFACT_NAMES[0], "/opt/flink/lib/" + ARTIFACT_NAMES[0]),
        _mount(artifact_root / ARTIFACT_NAMES[1], "/opt/flink/lib/" + ARTIFACT_NAMES[1]),
        _mount(artifact_root / ARTIFACT_NAMES[2], "/opt/flink/lib/" + ARTIFACT_NAMES[2]),
        _mount(artifact_root / ARTIFACT_NAMES[3], "/opt/flink/usrlib/dev-pipeline-job.jar"),
        _mount(PIPELINE_SQL, "/opt/flink/private/pipeline.sql"),
    ]
    common = {
        "cpus": 1,
        "entrypoint": None,
        "image": image,
        "labels": {"binhu.environment": "development"},
        "mem_limit": "805306368",
        "pids_limit": 256,
        "networks": {"internal": None},
        "volumes": mounts,
        "logging": {
            "driver": "json-file",
            "options": {"max-size": "5m", "max-file": "2"},
        },
    }
    return {
        "name": PROJECT,
        "networks": {
            "internal": {"name": NETWORK, "ipam": {}, "external": True},
        },
        "services": {
            "jobmanager": {
                **common,
                "command": ["jobmanager"],
                "environment": {
                    "JOB_MANAGER_RPC_ADDRESS": "jobmanager",
                    "APP_ENVIRONMENT": "development",
                    "FLINK_PROPERTIES": FLINK_PROPERTIES,
                },
            },
            "taskmanager": {
                **common,
                "command": ["taskmanager"],
                "environment": {
                    "JOB_MANAGER_RPC_ADDRESS": "jobmanager",
                    "TASK_MANAGER_NUMBER_OF_TASK_SLOTS": "2",
                    "APP_ENVIRONMENT": "development",
                    "FLINK_PROPERTIES": FLINK_PROPERTIES,
                },
            },
        },
        "volumes": {"flink-checkpoints": {"name": CHECKPOINT_VOLUME}},
    }


def _without_environment_labels(spec: dict) -> dict:
    value = copy.deepcopy(spec)
    for service in ("jobmanager", "taskmanager"):
        value["services"][service].pop("labels", None)
    return value


def measure(image: str, artifact_root: Path) -> dict:
    if TARGET.is_symlink() or TARGET.parent != ROOT or not TARGET.is_file():
        raise ValueError("fixed Dev Flink Compose file required")
    current = json.loads(TARGET.read_text(encoding="utf-8"))
    expected = specification(image, artifact_root)
    if _without_environment_labels(current) != _without_environment_labels(expected):
        raise ValueError("Dev Flink Compose differs beyond the approved labels")
    labels = {
        service: current["services"][service].get("labels", {}).get("binhu.environment")
        for service in ("jobmanager", "taskmanager")
    }
    return {
        "environment": "development",
        "project": PROJECT,
        "target": str(TARGET),
        "labels": labels,
        "requires_update": any(value != "development" for value in labels.values()),
        "other_changes": False,
    }


def apply(image: str, artifact_root: Path, evidence_id: str) -> dict:
    before = measure(image, artifact_root)
    if not re.fullmatch(r"dev-flink-label-[0-9a-f]{16}", evidence_id or ""):
        raise ValueError("fresh Dev Flink label evidence ID required")
    evidence = EVIDENCE_ROOT / evidence_id
    if evidence.exists() or evidence.is_symlink():
        raise ValueError("evidence directory already exists")
    evidence.mkdir(mode=0o700)
    original = TARGET.read_bytes()
    backup = evidence / "flink-pipeline-compose.before.json"
    backup.write_bytes(original)
    backup.chmod(0o600)
    expected = specification(image, artifact_root)
    payload = (json.dumps(expected, ensure_ascii=False, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".flink-compose-", delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    try:
        temporary.chmod(0o600)
        os.replace(temporary, TARGET)
    finally:
        temporary.unlink(missing_ok=True)
    after = measure(image, artifact_root)
    report = {
        "environment": "development",
        "project": PROJECT,
        "evidence_id": evidence_id,
        "before_sha256": hashlib.sha256(original).hexdigest(),
        "after_sha256": hashlib.sha256(payload).hexdigest(),
        "labels_added": before["requires_update"],
        "verified": not after["requires_update"],
    }
    target = evidence / "result.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("measure", "apply", "verify"))
    parser.add_argument("--image", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--evidence-id")
    args = parser.parse_args()
    try:
        if args.action == "apply":
            result = apply(args.image, args.artifact_root, args.evidence_id or "")
        else:
            result = measure(args.image, args.artifact_root)
            if args.action == "verify" and result["requires_update"]:
                raise ValueError("Dev Flink environment labels are incomplete")
        print(json.dumps(result))
    except (ValueError, OSError, KeyError, json.JSONDecodeError):
        raise SystemExit("Dev Flink Compose label update refused; preserve current resources") from None


if __name__ == "__main__":
    main()
