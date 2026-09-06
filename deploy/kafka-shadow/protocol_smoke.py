#!/usr/bin/env python3
"""Run a synthetic Kafka protocol and broker-failover smoke test.

The command is deliberately scoped to one isolated event-bus shadow project.
It never creates business records, contacts a production target, or removes a
Docker resource.  All Docker and Kafka commands have a finite timeout and are
sent through an injectable runner so the safety checks can be tested without a
Docker daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEFAULT_COMMAND_TIMEOUT = 45.0
LEADER_STOP_SECONDS = 30.0
ISR_WAIT_SECONDS = 90.0
ISR_POLL_SECONDS = 2.0
ARTIFACT_DIRNAME = "artifacts"
KAFKA_SERVICES = ("kafka-1", "kafka-2", "kafka-3")
EXPECTED_SERVICES = KAFKA_SERVICES + ("schema-registry",)
KAFKA_CLI_DIR = "/opt/kafka/bin"
KAFKA_BOOTSTRAP = "kafka-1:9092"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PROJECT_PATTERN = re.compile(r"^binhu-kafka-shadow-[A-Za-z0-9][A-Za-z0-9_.-]*$")
NETWORK_PATTERN = re.compile(
    r"^(?:binhu-kafka-shadow-[A-Za-z0-9][A-Za-z0-9_.-]*|"
    r"binhu_kafka_shadow_[A-Za-z0-9][A-Za-z0-9_.-]*)$"
)
PROJECT_DIR_PREFIX = "binhu-eventbus-shadow-"
FORBIDDEN_PRODUCTION = ("production", "prod")
FORBIDDEN_LOADTEST = ("loadtest", "locust", "flink-poc", "stress", "benchmark")

Runner = Callable[..., Any]


class SmokeError(RuntimeError):
    """A fail-closed protocol smoke error without command output leakage."""


@dataclass(frozen=True)
class Context:
    project_dir: Path
    env_path: Path
    compose_file: Path
    run_id: str
    attempt: int
    project: str
    network: str
    topic: str
    artifact_dir: Path
    identity_path: Path
    result_path: Path
    log_path: Path


@dataclass
class Recorder:
    commands: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        phase: str,
        args: Sequence[str],
        returncode: int,
        *,
        stdout: str,
        stderr: str,
    ) -> None:
        self.commands.append(
            {
                "phase": phase,
                "command": list(args),
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        )


def default_runner(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute one bounded command.  No shell is used."""

    return subprocess.run(
        list(args),
        cwd=str(cwd),
        timeout=timeout,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid .env line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid .env key on line {line_number}")
        if key in values:
            raise ValueError(f"duplicate .env key {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"invalid .env value for {key}")
        values[key] = value
    return values


def _parse_identity(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("deployment identity is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("deployment identity must be a JSON object")
    result: dict[str, str] = {}
    aliases = {
        "run_id": ("run_id", "KAFKA_RUN_ID", "kafka_run_id"),
        "project": (
            "project",
            "compose_project",
            "KAFKA_PROJECT",
            "kafka_project",
        ),
        "network": ("network", "KAFKA_NETWORK", "kafka_network"),
    }
    for target, keys in aliases.items():
        matches = [value[key] for key in keys if key in value]
        if (
            not matches
            or not all(isinstance(match, str) and match for match in matches)
            or len(set(matches)) != 1
        ):
            raise ValueError(f"deployment identity is missing {target}")
        result[target] = matches[0]
    return result


def _reject_identity(value: str, label: str) -> None:
    folded = value.casefold().replace("_", "-")
    if any(token in folded for token in FORBIDDEN_LOADTEST):
        raise ValueError(f"old loadtest target is forbidden in {label}")
    if any(token in folded for token in FORBIDDEN_PRODUCTION):
        raise ValueError(f"production target is forbidden in {label}")


def _safe_name(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not pattern.fullmatch(value):
        raise ValueError(f"{label} is not an isolated shadow identity")
    _reject_identity(value, label)


def _topic_for_run(run_id: str, attempt: int = 1) -> str:
    suffix = run_id.casefold()
    attempt_suffix = "" if attempt == 1 else f".a{attempt:02d}"
    topic = f"binhu.shadow.protocol.{suffix}{attempt_suffix}"
    if len(topic) > 200:
        raise ValueError("run id is too long for the protocol topic")
    return topic


def validate_project(project_dir: Path, run_id: str, attempt: int = 1) -> Context:
    """Validate the filesystem and identity boundary before any Docker call."""

    project_dir = Path(project_dir).expanduser()
    if not project_dir.exists() or not project_dir.is_dir():
        raise NotADirectoryError(f"project directory must exist: {project_dir}")
    project_dir = project_dir.resolve()
    basename = project_dir.name
    if not basename.startswith(PROJECT_DIR_PREFIX):
        raise ValueError(
            f"project directory must start with {PROJECT_DIR_PREFIX}"
        )
    _reject_identity(basename, "project directory")
    _reject_identity(str(project_dir), "project path")

    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run id is not a safe isolated identifier")
    _reject_identity(run_id, "run id")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 9999:
        raise ValueError("attempt must be an integer from 1 through 9999")

    env_path = project_dir / ".env"
    compose_file = project_dir / "docker-compose.yml"
    if not env_path.is_file():
        raise FileNotFoundError(f"isolated project is missing .env: {env_path}")
    if not compose_file.is_file():
        raise FileNotFoundError(
            f"isolated project is missing docker-compose.yml: {compose_file}"
        )
    values = _parse_env(env_path)
    project = values.get("KAFKA_PROJECT", "")
    network = values.get("KAFKA_NETWORK", "")
    configured_run_id = values.get("KAFKA_RUN_ID", "")
    _safe_name(project, PROJECT_PATTERN, "KAFKA_PROJECT")
    _safe_name(network, NETWORK_PATTERN, "KAFKA_NETWORK")
    if configured_run_id != run_id:
        raise ValueError("run id does not match .env KAFKA_RUN_ID")

    artifact_dir = project_dir / ARTIFACT_DIRNAME
    identity_path = artifact_dir / "deployment-identity.json"
    if not identity_path.is_file():
        raise FileNotFoundError(f"isolated project is missing {identity_path}")
    identity = _parse_identity(identity_path)
    if identity != {"run_id": run_id, "project": project, "network": network}:
        raise ValueError("deployment identity does not match .env")
    if attempt == 1:
        result_path = artifact_dir / f"{run_id}-protocol-smoke.json"
        log_path = artifact_dir / f"{run_id}-protocol-smoke.log"
    else:
        result_path = artifact_dir / f"{run_id}-protocol-smoke-attempt-{attempt:02d}.json"
        log_path = artifact_dir / f"{run_id}-protocol-smoke-attempt-{attempt:02d}.log"
    return Context(
        project_dir=project_dir,
        env_path=env_path,
        compose_file=compose_file,
        run_id=run_id,
        attempt=attempt,
        project=project,
        network=network,
        topic=_topic_for_run(run_id, attempt),
        artifact_dir=artifact_dir,
        identity_path=identity_path,
        result_path=result_path,
        log_path=log_path,
    )


def _result_field(result: Any, field: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(field, default)
    return getattr(result, field, default)


def _result_text(result: Any, field: str) -> str:
    value = _result_field(result, field, "")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else str(value or "")


def _invoke_runner(
    runner: Runner,
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    input_text: str | None = None,
) -> Any:
    if input_text is None:
        return runner(list(args), cwd=cwd, timeout=timeout)
    return runner(list(args), cwd=cwd, timeout=timeout, input_text=input_text)


def _execute(
    context: Context,
    runner: Runner,
    recorder: Recorder,
    args: Sequence[str],
    *,
    phase: str,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    input_text: str | None = None,
) -> Any:
    if timeout <= 0:
        raise ValueError("command timeout must be positive")
    try:
        result = _invoke_runner(
            runner,
            args,
            cwd=context.project_dir,
            timeout=timeout,
            input_text=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        recorder.add(
            phase,
            args,
            -1,
            stdout=_result_text(exc, "output"),
            stderr=_result_text(exc, "stderr"),
        )
        raise SmokeError(f"{phase} command timed out") from exc
    except OSError as exc:
        recorder.add(phase, args, -1, stdout="", stderr=str(exc))
        raise SmokeError(f"{phase} command could not start") from exc
    returncode = _result_field(result, "returncode", None)
    if not isinstance(returncode, int):
        raise SmokeError(f"{phase} command returned no exit status")
    recorder.add(
        phase,
        args,
        returncode,
        stdout=_result_text(result, "stdout"),
        stderr=_result_text(result, "stderr"),
    )
    if returncode != 0:
        raise SmokeError(f"{phase} command failed with exit code {returncode}")
    return result


def _compose(context: Context, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(context.env_path),
        "-f",
        str(context.compose_file),
        "-p",
        context.project,
        *args,
    ]


def _decode_json_lines(text: str, *, label: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        raise SmokeError(f"{label} returned no JSON")
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            values = parsed
        elif isinstance(parsed, dict):
            values = [parsed]
        else:
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        values = []
        for line in stripped.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SmokeError(f"{label} returned invalid JSON") from exc
            values.append(value)
    if not all(isinstance(value, dict) for value in values):
        raise SmokeError(f"{label} returned non-object JSON")
    return values  # type: ignore[return-value]


def _state_is_running(value: Any) -> bool:
    state = str(value or "").casefold()
    return state == "running" or state == "up" or state.startswith("up ")


def _allowed_container_names(project: str, service: str) -> set[str]:
    # Compose v2 normally appends ``-1`` to a service container.  Accept the
    # explicitly named form as well because a deployment may pin a
    # container_name, while still requiring an exact project-scoped identity.
    return {f"{project}-{service}", f"{project}-{service}-1"}


def _parse_compose_ps(result: Any) -> dict[str, dict[str, Any]]:
    rows = _decode_json_lines(_result_text(result, "stdout"), label="compose ps")
    by_service: dict[str, dict[str, Any]] = {}
    for row in rows:
        service = row.get("Service") or row.get("service")
        name = row.get("Name") or row.get("name")
        if not isinstance(service, str) or not isinstance(name, str):
            raise SmokeError("compose ps omitted service identity")
        by_service[service] = dict(row)
        if name.lstrip("/") == "":
            raise SmokeError("compose ps returned an empty container name")
    if set(by_service) != set(EXPECTED_SERVICES):
        raise SmokeError("compose ps did not return the four isolated services")
    return by_service


def _labels(item: Mapping[str, Any]) -> Mapping[str, Any]:
    config = item.get("Config")
    if isinstance(config, Mapping) and isinstance(config.get("Labels"), Mapping):
        return config["Labels"]  # type: ignore[return-value]
    labels = item.get("Labels")
    return labels if isinstance(labels, Mapping) else {}


def _validate_mounts(context: Context, service: str, mounts: Any) -> None:
    if not isinstance(mounts, list):
        raise SmokeError(f"{service} inspect omitted mounts")
    expected: list[tuple[str, str, str | None]] = []
    if service in KAFKA_SERVICES:
        expected.append(
            (
                "volume",
                "/var/lib/kafka/data",
                f"{context.project}_{service}-data",
            )
        )
    actual: list[tuple[str, str, str | None]] = []
    for mount in mounts:
        if not isinstance(mount, Mapping):
            raise SmokeError(f"{service} has an invalid mount record")
        mount_type = str(mount.get("Type", ""))
        destination = str(mount.get("Destination", ""))
        name = mount.get("Name")
        actual.append((mount_type, destination, str(name) if name else None))
    if sorted(actual) != sorted(expected):
        raise SmokeError(f"{service} has an unexpected mount boundary")
    for mount_type, destination, name in actual:
        if mount_type == "volume" and name != f"{context.project}_{service}-data":
            raise SmokeError(f"{service} data volume is outside this project")


def _validate_tmpfs(service: str, host_config: Any) -> None:
    expected = (
        "/etc/kafka/secrets",
        "/mnt/shared/config",
    ) if service in KAFKA_SERVICES else ()
    if not isinstance(host_config, Mapping):
        raise SmokeError(f"{service} inspect omitted HostConfig")
    tmpfs = host_config.get("Tmpfs", {})
    if not isinstance(tmpfs, Mapping):
        raise SmokeError(f"{service} inspect has invalid HostConfig.Tmpfs")
    if set(str(path) for path in tmpfs) != set(expected):
        raise SmokeError(f"{service} has an unexpected tmpfs boundary")


def _validate_runtime(context: Context, runner: Runner, recorder: Recorder) -> None:
    _execute(
        context,
        runner,
        recorder,
        _compose(context, "config", "--quiet"),
        phase="compose-config",
    )
    ps_result = _execute(
        context,
        runner,
        recorder,
        _compose(context, "ps", "--all", "--format", "json"),
        phase="compose-ps",
    )
    ps = _parse_compose_ps(ps_result)
    for service in EXPECTED_SERVICES:
        row = ps[service]
        name = str(row.get("Name") or row.get("name") or "").lstrip("/")
        if name not in _allowed_container_names(context.project, service):
            raise SmokeError(f"{service} container is outside this project")
        if not _state_is_running(row.get("State") or row.get("state") or row.get("Status")):
            raise SmokeError(f"{service} is not running")
        _reject_identity(name, f"{service} container")

    names = [
        str(ps[service].get("Name") or ps[service].get("name") or "").lstrip("/")
        for service in EXPECTED_SERVICES
    ]
    inspect_result = _execute(
        context,
        runner,
        recorder,
        ["docker", "inspect", *names],
        phase="docker-inspect",
    )
    inspected = _decode_json_lines(_result_text(inspect_result, "stdout"), label="docker inspect")
    by_name: dict[str, Mapping[str, Any]] = {}
    for item in inspected:
        name = str(item.get("Name", "")).lstrip("/")
        if name:
            by_name[name] = item
    if set(by_name) != set(names):
        raise SmokeError("docker inspect returned an unexpected container set")
    for service, name in zip(EXPECTED_SERVICES, names):
        item = by_name[name]
        labels = _labels(item)
        required_labels = {
            "com.docker.compose.project": context.project,
            "com.docker.compose.service": service,
            "com.docker.compose.project.working_dir": str(context.project_dir),
            "binhu.shadow": "true",
            "binhu.shadow.run_id": context.run_id,
        }
        for key, expected in required_labels.items():
            if str(labels.get(key, "")) != expected:
                raise SmokeError(f"{service} label {key} is not isolated")
        _validate_mounts(context, service, item.get("Mounts"))
        _validate_tmpfs(service, item.get("HostConfig"))

        network_settings = item.get("NetworkSettings")
        if not isinstance(network_settings, Mapping):
            raise SmokeError(f"{service} inspect omitted NetworkSettings")
        attached_networks = network_settings.get("Networks")
        if not isinstance(attached_networks, Mapping) or set(attached_networks) != {
            context.network
        }:
            raise SmokeError(f"{service} is attached to an unexpected network")
        ports = network_settings.get("Ports")
        if isinstance(ports, Mapping):
            # Docker reports image EXPOSE entries as ``{"9092/tcp": null}``.
            # Only a non-empty binding means a host port was published.
            if any(binding not in (None, [], {}) for binding in ports.values()):
                raise SmokeError(f"{service} publishes a host port")
        elif ports not in (None, [], {}):
            raise SmokeError(f"{service} has an invalid port mapping")

    network_result = _execute(
        context,
        runner,
        recorder,
        ["docker", "network", "inspect", context.network],
        phase="docker-network-inspect",
    )
    networks = _decode_json_lines(
        _result_text(network_result, "stdout"), label="docker network inspect"
    )
    if len(networks) != 1:
        raise SmokeError("network inspect returned an unexpected network set")
    network = networks[0]
    if str(network.get("Name", "")) != context.network:
        raise SmokeError("network identity does not match the isolated project")
    if network.get("Internal") is not True:
        raise SmokeError("network is not marked internal")
    labels = network.get("Labels")
    if not isinstance(labels, Mapping):
        raise SmokeError("network inspect omitted labels")
    if str(labels.get("com.docker.compose.project", "")) != context.project:
        raise SmokeError("network project label is not isolated")
    if str(labels.get("com.docker.compose.network", "")) != "internal":
        raise SmokeError("network is not the isolated internal network")
    network_containers = network.get("Containers")
    if not isinstance(network_containers, Mapping):
        raise SmokeError("network inspect omitted connected containers")
    connected_names = {
        str(item.get("Name", "")).lstrip("/")
        for item in network_containers.values()
        if isinstance(item, Mapping)
    }
    expected_names = {
        name
        for service, name in zip(EXPECTED_SERVICES, names)
        if name in _allowed_container_names(context.project, service)
    }
    if connected_names != expected_names:
        raise SmokeError("network contains an unexpected container set")
    _reject_identity(context.network, "network")


def _kafka_exec(context: Context, service: str, executable: str, *args: str) -> list[str]:
    return _compose(
        context,
        "exec",
        "-T",
        service,
        f"{KAFKA_CLI_DIR}/{executable}",
        *args,
    )


def _topic_describe(
    context: Context,
    runner: Runner,
    recorder: Recorder,
    *,
    service: str = "kafka-1",
) -> str:
    result = _execute(
        context,
        runner,
        recorder,
        _kafka_exec(
            context,
            service,
            "kafka-topics.sh",
            "--bootstrap-server",
            f"{service}:9092",
            "--describe",
            "--topic",
            context.topic,
        ),
        phase="topic-describe",
    )
    return _result_text(result, "stdout")


def _verify_topic_description(text: str, *, require_full_isr: bool) -> None:
    header = re.search(
        r"PartitionCount:\s*(\d+)\s+ReplicationFactor:\s*(\d+)", text
    )
    if not header or header.group(1) != "3" or header.group(2) != "2":
        raise SmokeError("protocol topic does not have exactly 3 partitions and RF 2")
    partitions: dict[int, tuple[set[int], set[int]]] = {}
    pattern = re.compile(
        r"Partition:\s*(\d+).*?Replicas:\s*([0-9,]+).*?Isr:\s*([0-9,]+)"
    )
    for match in pattern.finditer(text):
        partition = int(match.group(1))
        replicas = {int(value) for value in match.group(2).split(",")}
        isr = {int(value) for value in match.group(3).split(",")}
        partitions[partition] = (replicas, isr)
    if set(partitions) != {0, 1, 2}:
        raise SmokeError("protocol topic did not describe all three partitions")
    for replicas, isr in partitions.values():
        if len(replicas) != 2:
            raise SmokeError("protocol topic has a partition with the wrong replica count")
        if require_full_isr and isr != replicas:
            raise SmokeError("protocol topic ISR has not fully recovered")


def synthetic_records(
    run_id: str,
    *,
    start_sequence: int = 1,
    count: int = 3,
) -> list[dict[str, Any]]:
    """Return metadata-only records that contain no business data."""

    return [
        {
            "event_id": f"{run_id.casefold()}-protocol-{sequence:02d}",
            "operation": "protocol-smoke",
            "revision": sequence,
            "run_id": run_id,
            "sequence": sequence,
        }
        for sequence in range(start_sequence, start_sequence + count)
    ]


def _canonical_records(records: Sequence[Mapping[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
        for record in records
    )


def _produce(
    context: Context,
    runner: Runner,
    recorder: Recorder,
    *,
    records: Sequence[Mapping[str, Any]],
    phase: str,
) -> None:
    payload = _canonical_records(records)
    _execute(
        context,
        runner,
        recorder,
        _kafka_exec(
            context,
            "kafka-1",
            "kafka-console-producer.sh",
            "--bootstrap-server",
            KAFKA_BOOTSTRAP,
            "--topic",
            context.topic,
            "--producer-property",
            "acks=all",
        ),
        phase=phase,
        input_text=payload,
    )


def _consume(
    context: Context,
    runner: Runner,
    recorder: Recorder,
    *,
    expected_records: Sequence[Mapping[str, Any]],
    phase: str,
    group_suffix: str,
) -> None:
    attempt_suffix = "" if context.attempt == 1 else f"-attempt-{context.attempt:02d}"
    group = f"binhu-shadow-protocol-{context.run_id.casefold()}{attempt_suffix}-{group_suffix}"
    result = _execute(
        context,
        runner,
        recorder,
        _kafka_exec(
            context,
            "kafka-1",
            "kafka-console-consumer.sh",
            "--bootstrap-server",
            KAFKA_BOOTSTRAP,
            "--topic",
            context.topic,
            "--group",
            group,
            "--from-beginning",
            "--max-messages",
            str(len(expected_records)),
            "--timeout-ms",
            "10000",
            "--consumer-property",
            "enable.auto.commit=false",
        ),
        phase=phase,
    )
    lines = [line.strip() for line in _result_text(result, "stdout").splitlines() if line.strip()]
    try:
        actual = [json.loads(line) for line in lines]
    except json.JSONDecodeError as exc:
        raise SmokeError(f"{phase} returned invalid synthetic JSON") from exc
    expected = list(expected_records)
    # Kafka can interleave records from different partitions.  Protocol
    # equality is therefore by the immutable event id, not global line order.
    try:
        actual_sorted = sorted(actual, key=lambda record: record["event_id"])
        expected_sorted = sorted(expected, key=lambda record: record["event_id"])
    except (KeyError, TypeError) as exc:
        raise SmokeError(f"{phase} returned a malformed synthetic record") from exc
    if actual_sorted != expected_sorted:
        raise SmokeError(f"{phase} did not exactly match synthetic records")


def _produce_and_consume(
    context: Context,
    runner: Runner,
    recorder: Recorder,
    *,
    phase_suffix: str,
) -> None:
    records = synthetic_records(context.run_id)
    _produce(
        context,
        runner,
        recorder,
        records=records,
        phase=f"produce-{phase_suffix}",
    )
    _consume(
        context,
        runner,
        recorder,
        expected_records=records,
        phase=f"consume-{phase_suffix}",
        group_suffix=phase_suffix,
    )


def _quorum_status(
    context: Context,
    runner: Runner,
    recorder: Recorder,
    *,
    service: str,
    phase: str,
) -> tuple[int, set[int]]:
    result = _execute(
        context,
        runner,
        recorder,
        _kafka_exec(
            context,
            service,
            "kafka-metadata-quorum.sh",
            "--bootstrap-server",
            f"{service}:9092",
            "describe",
            "--status",
        ),
        phase=phase,
    )
    text = _result_text(result, "stdout")
    leader_match = re.search(r"(?:LeaderId|Leader):\s*(\d+)", text, re.IGNORECASE)
    voters: list[Any] | None = None
    for line in text.splitlines():
        marker = re.search(r"CurrentVoters:\s*", line, re.IGNORECASE)
        if not marker:
            continue
        fragment = line[marker.end():].strip()
        try:
            value = json.loads(fragment)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            voters = value
            break
    if not leader_match or voters is None:
        raise SmokeError(f"{phase} omitted KRaft leader or voter status")
    leader = int(leader_match.group(1))
    voter_ids: set[int] = set()
    for voter in voters:
        if isinstance(voter, Mapping) and isinstance(voter.get("id"), int):
            voter_ids.add(voter["id"])
        elif isinstance(voter, int):
            voter_ids.add(voter)
    if leader not in {1, 2, 3} or len(voter_ids) < 3 or not {1, 2, 3}.issubset(voter_ids):
        raise SmokeError(f"{phase} did not report the isolated three-node quorum")
    return leader, voter_ids


def _verify_surviving_quorum(
    context: Context,
    runner: Runner,
    recorder: Recorder,
    *,
    original_leader: int,
) -> None:
    ps_result = _execute(
        context,
        runner,
        recorder,
        _compose(context, "ps", "--all", "--format", "json"),
        phase="quorum-ps",
    )
    ps = _parse_compose_ps(ps_result)
    stopped_service = f"kafka-{original_leader}"
    if _state_is_running(ps[stopped_service].get("State") or ps[stopped_service].get("Status")):
        raise SmokeError("original leader remained running after stop")
    for service in KAFKA_SERVICES:
        if service != stopped_service and not _state_is_running(
            ps[service].get("State") or ps[service].get("Status")
        ):
            raise SmokeError("a surviving broker is not running")
    survivor = next(service for service in KAFKA_SERVICES if service != stopped_service)
    leader, voters = _quorum_status(
        context,
        runner,
        recorder,
        service=survivor,
        phase="quorum-after-stop",
    )
    if leader == original_leader or not {1, 2, 3}.issubset(voters):
        raise SmokeError("surviving brokers did not form a new KRaft quorum")


def _wait_for_full_isr(
    context: Context,
    runner: Runner,
    recorder: Recorder,
    *,
    sleep_fn: Callable[[float], None],
    clock_fn: Callable[[], float],
) -> None:
    deadline = clock_fn() + ISR_WAIT_SECONDS
    last_error: SmokeError | None = None
    while True:
        try:
            text = _topic_describe(context, runner, recorder)
            _verify_topic_description(text, require_full_isr=True)
            return
        except SmokeError as exc:
            last_error = exc
            if clock_fn() >= deadline:
                raise SmokeError(
                    "protocol topic ISR did not recover before timeout"
                ) from last_error
            sleep_fn(ISR_POLL_SECONDS)


def _publish_no_overwrite(paths_and_bytes: Sequence[tuple[Path, bytes]]) -> None:
    if not paths_and_bytes:
        return
    parent = paths_and_bytes[0][0].parent
    if any(path.parent != parent for path, _ in paths_and_bytes):
        raise ValueError("artifact files must share one directory")
    if any(os.path.lexists(path) for path, _ in paths_and_bytes):
        raise FileExistsError("refusing to overwrite an existing protocol artifact")
    temporary: list[Path] = []
    published: list[tuple[Path, tuple[int, int]]] = []
    try:
        for index, (_path, content) in enumerate(paths_and_bytes):
            fd, raw_path = tempfile.mkstemp(
                prefix=f".protocol-smoke-{index}-",
                suffix=".tmp",
                dir=parent,
            )
            temp_path = Path(raw_path)
            temporary.append(temp_path)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for temp_path, (target, _content) in zip(temporary, paths_and_bytes):
            stat = temp_path.stat()
            os.link(temp_path, target)
            published.append((target, (stat.st_dev, stat.st_ino)))
            temp_path.unlink()
    except Exception:
        for target, identity in published:
            try:
                stat = target.stat()
                if (stat.st_dev, stat.st_ino) == identity:
                    target.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for temp_path in temporary:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _write_artifacts(context: Context, result: Mapping[str, Any], recorder: Recorder) -> None:
    context.artifact_dir.mkdir(parents=False, exist_ok=True)
    payload = (json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    log = "\n".join(
        json.dumps(command, ensure_ascii=True, sort_keys=True)
        for command in recorder.commands
    ) + ("\n" if recorder.commands else "")
    _publish_no_overwrite(
        (
            (context.result_path, payload),
            (context.log_path, log.encode("utf-8")),
        )
    )


def run_smoke(
    context: Context,
    *,
    runner: Runner = default_runner,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the bounded smoke test and publish one immutable result pair."""

    context.artifact_dir.mkdir(parents=False, exist_ok=True)
    if os.path.lexists(context.result_path) or os.path.lexists(context.log_path):
        raise FileExistsError("refusing to overwrite an existing protocol artifact")
    recorder = Recorder()
    result: dict[str, Any] = {
        "schema": 1,
        "status": "failed",
        "run_id": context.run_id,
        "project": context.project,
        "network": context.network,
        "topic": context.topic,
        "leader_broker": None,
        "leader_restored": False,
        "isr_recovered": False,
        "commands": recorder.commands,
    }
    error: Exception | None = None
    original_leader: int | None = None
    try:
        _validate_runtime(context, runner, recorder)
        _execute(
            context,
            runner,
            recorder,
            _kafka_exec(
                context,
                "kafka-1",
                "kafka-topics.sh",
                "--bootstrap-server",
                KAFKA_BOOTSTRAP,
                "--create",
                "--if-not-exists",
                "--topic",
                context.topic,
                "--partitions",
                "3",
                "--replication-factor",
                "2",
                "--config",
                "min.insync.replicas=2",
            ),
            phase="topic-create",
        )
        _verify_topic_description(
            _topic_describe(context, runner, recorder), require_full_isr=True
        )
        _produce_and_consume(context, runner, recorder, phase_suffix="initial")
        original_leader, _voters = _quorum_status(
            context,
            runner,
            recorder,
            service="kafka-1",
            phase="quorum-before-stop",
        )
        result["leader_broker"] = original_leader

        failover_error: Exception | None = None
        try:
            _execute(
                context,
                runner,
                recorder,
                _compose(context, "stop", f"kafka-{original_leader}"),
                phase="stop-original-leader",
                timeout=LEADER_STOP_SECONDS + DEFAULT_COMMAND_TIMEOUT,
            )
            sleep_fn(LEADER_STOP_SECONDS)
            _verify_surviving_quorum(
                context,
                runner,
                recorder,
                original_leader=original_leader,
            )
        except Exception as exc:
            failover_error = exc
        finally:
            try:
                _execute(
                    context,
                    runner,
                    recorder,
                    _compose(context, "start", f"kafka-{original_leader}"),
                    phase="restore-original-leader",
                    timeout=DEFAULT_COMMAND_TIMEOUT,
                )
                result["leader_restored"] = True
            except Exception as restore_error:
                result["leader_restored"] = False
                if failover_error is None:
                    failover_error = restore_error
                else:
                    failover_error = SmokeError(
                        "leader restoration failed after a failover error"
                    )
        if failover_error is not None:
            raise failover_error

        _wait_for_full_isr(
            context,
            runner,
            recorder,
            sleep_fn=sleep_fn,
            clock_fn=clock_fn,
        )
        result["isr_recovered"] = True
        initial_records = synthetic_records(context.run_id)
        _consume(
            context,
            runner,
            recorder,
            expected_records=initial_records,
            phase="consume-old-after-recovery",
            group_suffix="old-after-recovery",
        )
        new_records = synthetic_records(context.run_id, start_sequence=4)
        _produce(
            context,
            runner,
            recorder,
            records=new_records,
            phase="produce-recovered-new",
        )
        _consume(
            context,
            runner,
            recorder,
            expected_records=initial_records + new_records,
            phase="consume-recovered",
            group_suffix="recovered",
        )
        result["status"] = "passed"
    except Exception as exc:
        error = exc
        result["error"] = str(exc)
    finally:
        result["commands"] = recorder.commands
        _write_artifacts(context, result, recorder)
    if error is not None:
        raise error
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--attempt",
        default=1,
        type=int,
        help="immutable retry number; values above 1 use a new topic/group/artifact suffix",
    )
    args = parser.parse_args(argv)
    try:
        context = validate_project(args.project_dir, args.run_id, args.attempt)
        result = run_smoke(context)
    except (OSError, ValueError, SmokeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"result": str(context.result_path), "status": result["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
