#!/usr/bin/env python3
"""Run the synthetic business workload inside an isolated Locust container.

The host only performs the read-only identity inspection and starts Docker.
Locust reaches the shadow gateway through the inspected internal network; it is
never started as a host process and receives no host environment or Docker
socket.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from business_guard import (
    BusinessGuardError,
    validate_project,
    validate_run_id,
    validate_network,
    validate_saved_identities,
)
from inspect_business_runtime import (
    RuntimeInspectionError,
    collect_runtime,
    validate_current_root,
)


ROOT = Path(__file__).resolve().parent
PROFILE_NAMES = ("smoke", "burst75")
FIXED_GATEWAY_URL = "http://shadow-gateway:8080"
IMAGE_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)(?::[0-9]{1,5})?"
    r"(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)*@sha256:[0-9a-f]{64}$"
)
DURATION_RE = re.compile(r"^[1-9][0-9]*(?:s|m)$")
MAX_DURATION_SECONDS = 300
MAX_RUN_TIMEOUT_SECONDS = 420
DOCKER_COMMAND_TIMEOUT_SECONDS = 30.0
CONTAINER_STOP_TIMEOUT_SECONDS = 15
RUNTIME_INDEX_KEYS = {
    "schema_version",
    "run_id",
    "fictional_only",
    "runtime_snapshot_sha256",
    "tasks",
}
PARSER_TYPES = {
    "全链条",
    "出租房屋核查",
    "寄递业",
    "疑似返苏",
    "苏州涉警",
    "交通涉警",
}
RUNTIME_TASK_KEYS = {
    "ordinal",
    "parser_type",
    "row_key",
    "source_id",
    "initial_revision",
    "scenario",
    "property_id",
    "property_version",
    "community",
    "inspector",
    "property_candidates",
}
RUNTIME_CANDIDATE_KEYS = {"property_id", "property_version"}
RUNTIME_SCENARIOS = {
    "assigned",
    "unassigned",
    "pending_registration",
    "conflict",
    "completed",
    "unverifiable",
}
SYNTHETIC_COMMUNITY_RE = re.compile(r"^压测社区[0-9]{2}$")
SYNTHETIC_INSPECTOR_RE = re.compile(
    r"^压测(?:组员|突发组员|组长|基础管控|管理员|超级管理员)[0-9]{2}$"
)
FORBIDDEN_RUNTIME_KEYS = {
    "name",
    "person_name",
    "identity_number",
    "id_card",
    "phone",
    "mobile",
    "address",
    "original_address",
    "values",
    "password",
    "token",
    "source_ref",
    "source_kind",
}

PopenFactory = Callable[..., Any]
CommandRunner = Callable[..., Any]
Collector = Callable[..., Mapping[str, Any]]


class BusinessRunError(RuntimeError):
    """Raised before a workload starts when its safety proof is incomplete."""


@dataclass(frozen=True)
class RunProfile:
    name: str
    users: int
    duration: str
    duration_seconds: int
    spawn_rate: int
    burst: bool


def _fail(message: str) -> None:
    raise BusinessRunError(message)


def _duration_seconds(value: str) -> int:
    if not isinstance(value, str) or DURATION_RE.fullmatch(value) is None:
        _fail("duration must be a positive integer number of seconds or minutes")
    amount = int(value[:-1])
    seconds = amount if value[-1] == "s" else amount * 60
    if seconds > MAX_DURATION_SECONDS:
        _fail("business run duration cannot exceed 300 seconds")
    return seconds


def resolve_profile(
    name: str = "smoke",
    *,
    users: int | None = None,
    duration: str | None = None,
) -> RunProfile:
    if name not in PROFILE_NAMES:
        _fail("profile must be smoke or burst75")
    defaults = {
        "smoke": (5, "300s", 1, False),
        "burst75": (75, "300s", 15, True),
    }
    default_users, default_duration, spawn_rate, burst = defaults[name]
    selected_users = default_users if users is None else users
    selected_duration = default_duration if duration is None else duration
    if selected_users != default_users:
        _fail(f"{name} profile requires exactly {default_users} users")
    duration_seconds = _duration_seconds(selected_duration)
    if name == "burst75" and selected_duration != "300s":
        _fail("burst75 profile requires exactly 300 seconds")
    return RunProfile(name, selected_users, selected_duration, duration_seconds, spawn_rate, burst)


def validate_base_url(value: str, project: str = "") -> str:
    """Require the only origin reachable by the workload container."""

    if value != FIXED_GATEWAY_URL:
        _fail("business runner only accepts http://shadow-gateway:8080")
    return FIXED_GATEWAY_URL


def validate_locust_image(value: str) -> str:
    if not isinstance(value, str) or IMAGE_RE.fullmatch(value) is None:
        _fail("Locust image must be pinned to a sha256 digest")
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _ensure_inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise BusinessRunError(f"{label} must be inside the shadow root") from exc
    return resolved


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessRunError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        _fail(f"{label} must be a JSON object")
    return value


def _validate_row_key(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        _fail("runtime row_key is malformed")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("runtime row_key contains a control character")
    return value


def _validate_property_candidates(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        _fail("runtime property_candidates is malformed")
    for candidate in value:
        if not isinstance(candidate, Mapping):
            _fail("runtime property candidate is malformed")
        if set(candidate) - RUNTIME_CANDIDATE_KEYS:
            _fail("runtime property candidate contains an unknown field")
        for key in RUNTIME_CANDIDATE_KEYS:
            number = candidate.get(key)
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                _fail(f"runtime property candidate {key} is malformed")


def validate_runtime_index(
    path: Path,
    run_id: str,
    snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate the sanitized synthetic index; provenance is format checked only.

    The index is exported from a separate read-only verify snapshot.  Its hash
    is retained as provenance, but it is not compared with the new run
    preflight hash: a fresh run snapshot is required independently.
    """

    try:
        expected_run = validate_run_id(run_id)
    except BusinessGuardError as exc:
        raise BusinessRunError(str(exc)) from exc
    data = dict(_read_json(path, "runtime index"))
    if set(data) - RUNTIME_INDEX_KEYS:
        _fail("runtime index contains an unknown top-level field")
    if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
        _fail("runtime index schema_version is unsupported")
    if data.get("run_id") != expected_run:
        _fail("runtime index run_id does not match the requested run")
    if data.get("fictional_only") is not True:
        _fail("runtime index must be explicitly fictional_only")
    provenance = data.get("runtime_snapshot_sha256")
    if not isinstance(provenance, str) or re.fullmatch(r"[0-9a-f]{64}", provenance) is None:
        _fail("runtime index runtime_snapshot_sha256 is malformed")
    if snapshot_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256) is None:
        _fail("fresh runtime snapshot hash is malformed")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        _fail("runtime index contains no tasks")
    seen_rows: set[tuple[str, str]] = set()
    seen_sources: set[int] = set()
    seen_ordinals: set[int] = set()
    for task in tasks:
        if not isinstance(task, Mapping):
            _fail("runtime index task is malformed")
        keys = {str(key).casefold() for key in task}
        if keys & FORBIDDEN_RUNTIME_KEYS:
            _fail("runtime index contains business identity or credential data")
        if set(task) - RUNTIME_TASK_KEYS:
            _fail("runtime index task contains an unknown field")
        parser_type = task.get("parser_type")
        if parser_type not in PARSER_TYPES:
            _fail("runtime index parser_type is unsupported")
        row_key = _validate_row_key(task.get("row_key"))
        source_id = task.get("source_id")
        if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
            _fail("runtime index source_id is malformed")
        row_identity = (str(parser_type), row_key)
        if row_identity in seen_rows:
            _fail("runtime index contains a duplicate parser_type and row_key")
        if source_id in seen_sources:
            _fail("runtime index contains a duplicate source_id")
        seen_rows.add(row_identity)
        seen_sources.add(source_id)
        scenario = task.get("scenario", "assigned")
        if scenario not in RUNTIME_SCENARIOS:
            _fail("runtime index scenario is unsupported")
        for key in ("ordinal", "initial_revision", "property_id", "property_version"):
            if key not in task or task[key] is None:
                continue
            number = task[key]
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                _fail(f"runtime index {key} is malformed")
        ordinal = task.get("ordinal")
        if ordinal is not None:
            if ordinal in seen_ordinals:
                _fail("runtime index contains a duplicate ordinal")
            seen_ordinals.add(ordinal)
        for key in ("community", "inspector"):
            if key in task and task[key] is not None and not isinstance(task[key], str):
                _fail(f"runtime index {key} is malformed")
        community = task.get("community")
        if community is not None and SYNTHETIC_COMMUNITY_RE.fullmatch(community) is None:
            _fail("runtime index community is not a recognized synthetic value")
        inspector = task.get("inspector")
        if inspector not in (None, "") and SYNTHETIC_INSPECTOR_RE.fullmatch(inspector) is None:
            _fail("runtime index inspector is not a recognized synthetic value")
        _validate_property_candidates(task.get("property_candidates"))
    return data


def _default_locustfile(root: Path) -> Path:
    candidate = root / "load-tests" / "locustfile.py"
    if not candidate.is_file():
        _fail("shadow root is missing its shared load-tests/locustfile.py")
    return candidate.resolve()


def _validate_locustfile(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    expected_parent = (root / "load-tests").resolve()
    if resolved.parent != expected_parent or resolved.name != "locustfile.py" or not resolved.is_file():
        _fail("locustfile must be the shared shadow-root/load-tests/locustfile.py")
    return resolved


def _default_runtime_index(root: Path, run_id: str) -> Path:
    candidates = (
        root / "artifacts" / f"business-runtime-index-{run_id}.json",
        root / "artifacts" / f"shadow-runtime-{run_id}.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    _fail("synthetic business runtime index is missing; run the business seeder first")


def build_locust_command(
    locust_executable: str,
    locustfile: Path,
    base_url: str,
    profile: RunProfile,
    output_prefix: Path,
    event_log: Path,
    runtime_index: Path,
) -> list[str]:
    """Build only the argv executed *inside* the fixed Locust image."""

    if locust_executable != "locust":
        _fail("Locust must run from the fixed container entrypoint")
    validate_base_url(base_url)
    return [
        locust_executable,
        "-f",
        str(locustfile),
        "--headless",
        "--users",
        str(profile.users),
        "--spawn-rate",
        str(profile.spawn_rate),
        "--run-time",
        profile.duration,
        "--host",
        FIXED_GATEWAY_URL,
        "--stop-timeout",
        "15",
        "--csv",
        str(output_prefix),
        "--csv-full-history",
        "--html",
        str(output_prefix.with_suffix(".html")),
    ]


def _container_env(
    *,
    run_id: str,
    project: str,
    profile: RunProfile,
    runtime_index_name: str,
    event_log_name: str,
) -> dict[str, str]:
    values = {
        "APP_ENVIRONMENT": "shadow",
        "KAFKA_RUN_ID": run_id,
        "LOAD_TEST_RUN_ID": run_id,
        "COMPOSE_PROJECT_NAME": project,
        "LOAD_TEST_SCENARIO": "mixed",
        "LOAD_TEST_PROFILE": profile.name,
        "LOAD_TEST_USERS": str(profile.users),
        "LOAD_TEST_DURATION": profile.duration,
        "SHADOW_RUNTIME_INDEX": f"/run-input/{runtime_index_name}",
        "SHADOW_EVENT_LOG": f"/run-artifacts/{event_log_name}",
    }
    if profile.burst:
        values["LOAD_TEST_BURST"] = "1"
    return values


def _mount(source: Path, destination: str, mode: str) -> str:
    return f"type=bind,src={source.as_posix()},dst={destination},{mode}"


def build_docker_run_command(
    *,
    network: str,
    container_name: str,
    run_id: str,
    project: str,
    locust_image: str,
    profile: RunProfile,
    locustfile: Path,
    runtime_index: Path,
    artifacts: Path,
    event_log: Path,
    output_prefix: Path,
) -> list[str]:
    """Build the complete, reviewable Docker argv for one owned container."""

    try:
        validate_run_id(run_id)
        validate_project(project)
        validate_network(network, project)
    except BusinessGuardError as exc:
        raise BusinessRunError(str(exc)) from exc
    if not container_name.startswith(f"kshadow-business-{run_id}-"):
        _fail("container name is not owned by the requested KSHADOW run")
    if "binhu-loadtest" in container_name:
        _fail("legacy load-test container names are forbidden")
    validate_locust_image(locust_image)
    locust_args = build_locust_command(
        "locust",
        Path("/load-tests/locustfile.py"),
        FIXED_GATEWAY_URL,
        profile,
        Path("/run-artifacts") / output_prefix.name,
        Path("/run-artifacts") / event_log.name,
        Path("/run-input") / runtime_index.name,
    )
    return [
        "docker",
        "run",
        "--pull",
        "never",
        "--network",
        network,
        "--name",
        container_name,
        "--label",
        "binhu.shadow=true",
        "--label",
        f"binhu.shadow.run_id={run_id}",
        "--label",
        f"binhu.shadow.project={project}",
        "--label",
        "binhu.shadow.scope=business",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--cpus",
        "2",
        "--memory",
        "1g",
        "--pids-limit",
        "256",
        "--mount",
        _mount(locustfile, "/load-tests/locustfile.py", "readonly"),
        "--mount",
        _mount(runtime_index, f"/run-input/{runtime_index.name}", "readonly"),
        "--mount",
        _mount(artifacts, "/run-artifacts", "rw"),
        locust_image,
        *locust_args,
    ]


def _docker_result_field(result: Any, field: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(field, default)
    return getattr(result, field, default)


def _run_docker_command(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
) -> Any:
    try:
        return runner(
            list(command),
            cwd=str(cwd),
            env={"PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError, TypeError) as exc:
        raise BusinessRunError("Docker command could not be executed") from exc


def _validate_local_locust_image(
    runner: CommandRunner, cwd: Path, timeout: float, locust_image: str
) -> None:
    validate_locust_image(locust_image)
    result = _run_docker_command(
        runner,
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", locust_image],
        cwd=cwd,
        timeout=timeout,
    )
    if _docker_result_field(result, "returncode", 1) != 0:
        _fail("fixed Locust image is not present locally; --pull never forbids fetching it")
    try:
        digests = json.loads(str(_docker_result_field(result, "stdout", "")))
    except json.JSONDecodeError as exc:
        raise BusinessRunError("local Locust image inspection returned invalid JSON") from exc
    if not isinstance(digests, list) or locust_image not in digests:
        _fail("local Locust image RepoDigest does not match the fixed digest")


def _best_effort_docker(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
) -> bool:
    try:
        result = _run_docker_command(runner, command, cwd=cwd, timeout=timeout)
    except BusinessRunError:
        return False
    return _docker_result_field(result, "returncode", 1) == 0


def _container_name(run_id: str, profile: RunProfile, stamp: str) -> str:
    return f"kshadow-business-{run_id}-{profile.name}-{stamp.replace('.', '-')}"


def _new_artifact_path(directory: Path, name: str) -> Path:
    path = directory / name
    if path.exists():
        _fail("run artifact already exists")
    return path


def _validate_snapshot(snapshot: Mapping[str, Any], run_id: str, root: Path) -> Any:
    if not isinstance(snapshot, Mapping):
        _fail("runtime identity snapshot must be a JSON object")
    if snapshot.get("status") != "passed" or snapshot.get("phase") != "run":
        _fail("runtime identity preflight did not pass for the run phase")
    if snapshot.get("run_id") != run_id:
        _fail("runtime identity snapshot run_id does not match")
    snapshot_root = snapshot.get("root")
    if not isinstance(snapshot_root, str) or snapshot_root.replace("\\", "/") != root.as_posix():
        _fail("runtime identity snapshot root does not match")
    supplied_hash = snapshot.get("sha256")
    if not isinstance(supplied_hash, str) or re.fullmatch(r"[0-9a-f]{64}", supplied_hash) is None:
        _fail("runtime identity snapshot hash is missing")
    actual_hash = hashlib.sha256(
        _canonical_bytes({key: value for key, value in snapshot.items() if key != "sha256"})
    ).hexdigest()
    if actual_hash != supplied_hash:
        _fail("runtime identity snapshot hash does not match")
    docker_identity = snapshot.get("docker_identity")
    db_identity = snapshot.get("db_identity")
    if not isinstance(docker_identity, Mapping) or not isinstance(db_identity, Mapping):
        _fail("runtime identity snapshot is incomplete")
    try:
        identity = validate_saved_identities(run_id, docker_identity, db_identity)
        validate_project(identity.project)
    except BusinessGuardError as exc:
        raise BusinessRunError(f"business shadow identity preflight failed: {exc}") from exc
    if snapshot.get("project") != identity.project:
        _fail("runtime identity snapshot project does not match")
    return identity


def run(
    run_id: str,
    *,
    profile: str = "smoke",
    users: int | None = None,
    duration: str | None = None,
    root: Path | None = None,
    runtime_index: Path | None = None,
    locustfile: Path | None = None,
    locust_image: str | None = None,
    base_url: str | None = None,
    output_dir: Path | None = None,
    preflight_timeout: float = 30.0,
    run_timeout: float | None = None,
    collector: Collector | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
    docker_runner: CommandRunner = subprocess.run,
    process_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Collect fresh identity, validate inputs, then run one owned container."""

    try:
        expected_run = validate_run_id(run_id)
    except BusinessGuardError as exc:
        raise BusinessRunError(str(exc)) from exc
    selected_profile = resolve_profile(profile, users=users, duration=duration)
    if isinstance(preflight_timeout, bool) or not isinstance(preflight_timeout, (int, float)) or not 0 < preflight_timeout <= 120:
        _fail("preflight timeout must be between 0 and 120 seconds")
    requested_root = (Path.cwd() if root is None else Path(root)).resolve()
    try:
        checked_root_text = validate_current_root(
            requested_root.as_posix(), Path.cwd().resolve().as_posix()
        )
    except (RuntimeInspectionError, BusinessGuardError) as exc:
        raise BusinessRunError(str(exc)) from exc
    checked_root = Path(checked_root_text)
    artifact_root = _ensure_inside(checked_root / "artifacts", checked_root, "artifacts directory")
    artifacts = artifact_root if output_dir is None else Path(output_dir).resolve()
    _ensure_inside(artifacts, artifact_root, "output directory")
    if not artifacts.is_dir():
        _fail("output directory must already exist")
    index_path = (
        _default_runtime_index(checked_root, expected_run)
        if runtime_index is None
        else Path(runtime_index).resolve()
    )
    _ensure_inside(index_path, artifact_root, "runtime index")
    shared_locustfile = (
        _default_locustfile(checked_root) if locustfile is None else Path(locustfile)
    )
    _ensure_inside(checked_root / "load-tests", checked_root, "load-tests directory")
    shared_locustfile = _validate_locustfile(shared_locustfile, checked_root)
    validate_base_url(FIXED_GATEWAY_URL if base_url is None else base_url)
    if locust_image is None:
        _fail("--locust-image is required and must be a digest-pinned local image")
    validate_locust_image(locust_image)

    # This is intentionally the first live operation.  Its snapshot is fresh
    # for this run phase; no saved artifact can substitute for it.
    try:
        collect = collect_runtime if collector is None else collector
        snapshot = collect(
            checked_root,
            run_id=expected_run,
            phase="run",
            timeout=preflight_timeout,
        )
    except (RuntimeInspectionError, BusinessGuardError, OSError) as exc:
        raise BusinessRunError(f"business shadow identity preflight failed: {exc}") from exc
    identity = _validate_snapshot(snapshot, expected_run, checked_root)
    validate_runtime_index(index_path, expected_run, str(snapshot["sha256"]))
    _validate_local_locust_image(docker_runner, checked_root, preflight_timeout, locust_image)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_artifact_dir = artifacts / f".business-{expected_run}-{selected_profile.name}-{stamp}"
    try:
        run_artifact_dir.mkdir(mode=0o700)
    except OSError as exc:
        raise BusinessRunError("run artifact directory could not be created") from exc
    prefix = _new_artifact_path(
        run_artifact_dir, f"business-{expected_run}-{selected_profile.name}-{stamp}"
    )
    event_log = _new_artifact_path(
        run_artifact_dir, f"business-events-{expected_run}-{selected_profile.name}-{stamp}.jsonl"
    )
    stdout_path = _new_artifact_path(
        run_artifact_dir, f"business-stdout-{expected_run}-{selected_profile.name}-{stamp}.log"
    )
    stderr_path = _new_artifact_path(
        run_artifact_dir, f"business-stderr-{expected_run}-{selected_profile.name}-{stamp}.log"
    )
    report_path = _new_artifact_path(
        run_artifact_dir, f"business-run-{expected_run}-{selected_profile.name}-{stamp}.json"
    )
    event_log.touch(exist_ok=False)
    container_name = _container_name(expected_run, selected_profile, stamp)
    command = build_docker_run_command(
        network=identity.network,
        container_name=container_name,
        run_id=expected_run,
        project=identity.project,
        locust_image=locust_image,
        profile=selected_profile,
        locustfile=shared_locustfile,
        runtime_index=index_path,
        artifacts=run_artifact_dir,
        event_log=event_log,
        output_prefix=prefix,
    )
    # Build the environment as an allowlist.  The command helper has no access
    # to os.environ and therefore cannot accidentally leak credentials.
    env_values = _container_env(
        run_id=expected_run,
        project=identity.project,
        profile=selected_profile,
        runtime_index_name=index_path.name,
        event_log_name=event_log.name,
    )
    env_args: list[str] = []
    for key, value in env_values.items():
        env_args.extend(("--env", f"{key}={value}"))
    image_index = command.index(locust_image)
    command[image_index:image_index] = env_args

    timeout_seconds = selected_profile.duration_seconds + 60 if run_timeout is None else run_timeout
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= MAX_RUN_TIMEOUT_SECONDS:
        _fail("run timeout must be between 0 and 420 seconds")
    return_code = 127
    timed_out = False
    error: str | None = None
    cleanup_ok = True
    process: Any = None
    try:
        with stdout_path.open("x", encoding="utf-8") as stdout, stderr_path.open(
            "x", encoding="utf-8"
        ) as stderr:
            try:
                if process_factory is not None:
                    process = process_factory()
                else:
                    process = popen_factory(
                        command,
                        cwd=str(checked_root),
                        env={"PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")},
                        shell=False,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                    )
                return_code = int(process.wait(timeout=timeout_seconds))
            except subprocess.TimeoutExpired:
                timed_out = True
                error = "Locust container exceeded its bounded run timeout"
                _best_effort_docker(
                    docker_runner,
                    ["docker", "stop", "--time", str(CONTAINER_STOP_TIMEOUT_SECONDS), container_name],
                    cwd=checked_root,
                    timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
                )
                try:
                    process.wait(timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    _best_effort_docker(
                        docker_runner,
                        ["docker", "kill", container_name],
                        cwd=checked_root,
                        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
                    )
                    try:
                        process.wait(timeout=10)
                    except (subprocess.TimeoutExpired, OSError):
                        if hasattr(process, "kill"):
                            process.kill()
                return_code = 124
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                error = "Locust container could not be started or waited for"
                return_code = 127
                if isinstance(exc, OSError):
                    error = "Locust container could not be started"
    finally:
        cleanup_ok = _best_effort_docker(
            docker_runner,
            ["docker", "rm", "-f", container_name],
            cwd=checked_root,
            timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
        )
    if not cleanup_ok and error is None:
        error = "owned Locust container could not be removed"
        return_code = 125
    report: dict[str, Any] = {
        "status": "completed" if return_code == 0 and not timed_out else "failed",
        "acceptance": "not_performed",
        "run_id": expected_run,
        "project": identity.project,
        "network": identity.network,
        "container_name": container_name,
        "locust_image": locust_image,
        "profile": selected_profile.name,
        "users": selected_profile.users,
        "duration": selected_profile.duration,
        "duration_seconds": selected_profile.duration_seconds,
        "burst": selected_profile.burst,
        "fictional_only": True,
        "timed_out": timed_out,
        "runtime_snapshot_sha256": str(snapshot["sha256"]),
        "runtime_index": str(index_path),
        "event_log": str(event_log),
        "container_stdout": str(stdout_path),
        "container_stderr": str(stderr_path),
        "csv_prefix": str(prefix),
        "html": str(prefix.with_suffix(".html")),
        "exit_code": return_code,
        "cleanup_ok": cleanup_ok,
    }
    if error is not None:
        report["error"] = error
    try:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise BusinessRunError("run report could not be written") from exc
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile", choices=PROFILE_NAMES, default="smoke")
    parser.add_argument("--users", type=int)
    parser.add_argument("--duration")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--runtime-index", type=Path)
    parser.add_argument("--locustfile", type=Path)
    parser.add_argument("--locust-image", required=True)
    parser.add_argument(
        "--base-url",
        help="deprecated compatibility option; must equal http://shadow-gateway:8080",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--preflight-timeout", type=float, default=30.0)
    parser.add_argument("--run-timeout", type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run(
            args.run_id,
            profile=args.profile,
            users=args.users,
            duration=args.duration,
            root=args.root,
            runtime_index=args.runtime_index,
            locustfile=args.locustfile,
            locust_image=args.locust_image,
            base_url=args.base_url,
            output_dir=args.output_dir,
            preflight_timeout=args.preflight_timeout,
            run_timeout=args.run_timeout,
        )
    except BusinessRunError as exc:
        print(f"business shadow run rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["exit_code"] == 0 else int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
