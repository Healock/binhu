#!/usr/bin/env python3
"""Collect a fresh, read-only identity snapshot for a Kafka business shadow.

The command is intended to run from the deployed project root on the shadow
host.  It reads the project's ``.env`` and deployment manifest, asks Docker
for the already-running project's configuration and inspect records, and
queries only ``_shadow_identity`` in the three declared databases.  It never
starts, stops, removes, or executes a workload container.  Secret values are
never copied into the resulting artifact or included in command errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from business_guard import (
    BusinessGuardError,
    DATABASE_RE,
    IMAGE_RE,
    PROJECT_RE,
    RUN_ID_RE,
    validate_saved_identities,
)


DEFAULT_TIMEOUT = 30.0
PROJECT_ROOT_RE = re.compile(r"^/srv/binhu-eventbus-shadow-[A-Za-z0-9][A-Za-z0-9_.-]*$")
COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.derived.yml",
    "docker-compose.business.yml",
)
FORBIDDEN_LIFECYCLE_COMMANDS = {
    "up",
    "down",
    "start",
    "stop",
    "restart",
    "rm",
    "run",
    "create",
    "kill",
    "pause",
    "unpause",
}
IDENTITY_ENV_KEYS = {
    "APP_ENVIRONMENT",
    "APP_ENVIRONMENT_LABEL",
    "COMPOSE_PROJECT_NAME",
    "KAFKA_RUN_ID",
    "LOAD_TEST_RUN_ID",
    "KAFKA_PROJECT",
    "KAFKA_NETWORK",
    "KAFKA_BOOTSTRAP_SERVERS",
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_DATABASE",
    "MYSQL_ONLINE_DATA_DB",
    "MYSQL_ARCHIVE_DB",
    "MYSQL_DAILY_REPORT_DB",
    "MYSQL_PLATFORM_DB",
    "MYSQL_VISIT_DB",
    "MYSQL_DISPATCH_DB",
    "MYSQL_REGISTRY_DB",
    "MYSQL_WORKFLOW_DB",
    "BUSINESS_ONLINE_DATABASE",
    "BUSINESS_ARCHIVE_DATABASE",
    "BUSINESS_DAILY_DATABASE",
}
IDENTITY_LABEL_KEYS = {
    "com.docker.compose.project",
    "com.docker.compose.service",
    "com.docker.compose.project.working_dir",
    "com.docker.compose.network",
    "binhu.shadow",
    "binhu.shadow.run_id",
    "binhu.shadow.scope",
}

Runner = Callable[..., Any]


class RuntimeInspectionError(RuntimeError):
    """Raised when a live snapshot cannot prove the requested boundary."""


@dataclass(frozen=True)
class ComposeIdentity:
    project: str
    network: str
    services: dict[str, str]
    volumes: dict[str, str]
    databases: dict[str, str]
    environment: dict[str, dict[str, str]]


def _fail(message: str) -> None:
    raise RuntimeInspectionError(message)


def _result_field(result: Any, field: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(field, default)
    return getattr(result, field, default)


def _result_text(result: Any, field: str) -> str:
    value = _result_field(result, field, "")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else str(value or "")


def default_runner(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded command without a host shell."""

    return subprocess.run(
        list(args),
        cwd=str(cwd),
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )


def _execute(
    runner: Runner,
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    phase: str,
) -> str:
    if timeout <= 0:
        _fail("command timeout must be positive")
    if any(token in FORBIDDEN_LIFECYCLE_COMMANDS for token in args):
        _fail(f"{phase} attempted a Docker lifecycle command")
    try:
        result = runner(list(args), cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeInspectionError(f"{phase} command timed out") from exc
    except OSError as exc:
        raise RuntimeInspectionError(f"{phase} command could not start") from exc
    returncode = _result_field(result, "returncode", None)
    if not isinstance(returncode, int):
        _fail(f"{phase} command returned no exit status")
    if returncode != 0:
        # Never surface Docker or MySQL stderr because it may contain env or
        # server details that are not part of the sanitized artifact.
        _fail(f"{phase} command failed with exit code {returncode}")
    return _result_text(result, "stdout")


def _parse_json(text: str, label: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeInspectionError(f"{label} returned invalid JSON") from exc


def _parse_json_lines(text: str, label: str) -> list[Mapping[str, Any]]:
    stripped = text.strip()
    if not stripped:
        _fail(f"{label} returned no records")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        values: list[Any] = []
        for line in stripped.splitlines():
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeInspectionError(f"{label} returned invalid JSON") from exc
    else:
        values = value if isinstance(value, list) else [value]
    if not all(isinstance(item, Mapping) for item in values):
        _fail(f"{label} returned a non-object record")
    return values  # type: ignore[return-value]


def validate_current_root(root: str | Path, cwd: str | Path | None = None) -> str:
    """Require the current server directory to be one eventbus shadow root."""

    root_text = str(root).replace("\\", "/")
    if PROJECT_ROOT_RE.fullmatch(root_text) is None:
        _fail("current directory must be /srv/binhu-eventbus-shadow-*")
    if cwd is not None:
        cwd_text = str(cwd).replace("\\", "/")
        if cwd_text != root_text:
            _fail("current working directory does not match the requested shadow root")
    return root_text


def parse_dotenv_identity(text: str) -> dict[str, str]:
    """Parse only non-secret identity keys from the project's ``.env``."""

    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            _fail(f".env line {line_number} is malformed")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            _fail(f".env key on line {line_number} is malformed")
        if key in result:
            _fail(f".env repeats identity key {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if "\x00" in value or "\n" in value or "\r" in value:
            _fail(f".env value on line {line_number} is malformed")
        if key in IDENTITY_ENV_KEYS or (
            (key.startswith("MYSQL_") or key.startswith("BUSINESS_"))
            and (key.endswith("_DB") or key.endswith("DATABASE"))
        ):
            result[key] = value
    return result


def _required_env_alias(identity: Mapping[str, str], keys: Sequence[str], label: str) -> str:
    """Require one non-empty value when an identity field has aliases."""

    values = [identity[key] for key in keys if key in identity]
    if not values or any(not value.strip() for value in values):
        _fail(f".env is missing {label}")
    if len(set(values)) != 1:
        _fail(f".env {label} aliases disagree")
    return values[0]


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} is missing")
    return value.strip()


def _manifest_services(manifest: Mapping[str, Any]) -> dict[str, str]:
    raw = manifest.get("services")
    if not isinstance(raw, Mapping) or not raw:
        _fail("deployment manifest is missing services")
    services: dict[str, str] = {}
    for service, value in raw.items():
        name = _required_string(service, "manifest service")
        if isinstance(value, Mapping):
            value = value.get("image")
        image = _required_string(value, f"manifest image for {name}")
        if IMAGE_RE.fullmatch(image) is None:
            _fail(f"manifest image for {name} is not digest pinned")
        services[name] = image
    return services


def _manifest_volumes(manifest: Mapping[str, Any], project: str) -> dict[str, str]:
    raw = manifest.get("volumes")
    if not isinstance(raw, Mapping) or not raw:
        _fail("deployment manifest is missing volumes")
    volumes: dict[str, str] = {}
    for logical, value in raw.items():
        logical_name = _required_string(logical, "manifest volume")
        if isinstance(value, Mapping):
            value = value.get("docker_name") or value.get("name")
        actual = _required_string(value, f"Docker volume for {logical_name}")
        if not actual.startswith(project + "_"):
            _fail("manifest volume is outside the Compose project")
        volumes[logical_name] = actual
    return volumes


def validate_manifest(
    manifest: Mapping[str, Any],
    root: str,
    expected_run_id: str | None = None,
) -> tuple[str, str, dict[str, str], dict[str, str]]:
    """Validate only non-secret deployment-manifest fields."""

    if not isinstance(manifest, Mapping):
        _fail("deployment manifest must be a JSON object")
    project = _required_string(manifest.get("project"), "manifest project")
    if PROJECT_RE.fullmatch(project) is None:
        _fail("manifest project is not an isolated Kafka shadow project")
    run_id = _required_string(manifest.get("run_id"), "manifest run_id")
    if RUN_ID_RE.fullmatch(run_id) is None:
        _fail("manifest run_id is not a KSHADOW run")
    if expected_run_id is not None and run_id != expected_run_id:
        _fail("manifest run_id does not match the requested run")
    manifest_root = _required_string(manifest.get("root"), "manifest root")
    if manifest_root != root:
        _fail("manifest root does not match the current directory")
    status = manifest.get("status")
    if status is not None and str(status).casefold() not in {"running", "healthy", "up"}:
        _fail("deployment manifest does not describe a running project")
    services = _manifest_services(manifest)
    volumes = _manifest_volumes(manifest, project)
    return project, run_id, services, volumes


def _env_mapping(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items()}
    if not isinstance(value, list):
        _fail(f"{label} environment is malformed")
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, str) or "=" not in item:
            _fail(f"{label} environment is malformed")
        key, item_value = item.split("=", 1)
        result[key] = item_value
    return result


def filter_identity_env(value: Mapping[str, Any] | Sequence[str]) -> dict[str, str]:
    """Keep only environment values needed to prove shadow identity."""

    env = _env_mapping(value, "container")
    result: dict[str, str] = {}
    for key, item in env.items():
        if key in IDENTITY_ENV_KEYS:
            result[key] = item
            continue
        # Permit future database aliases while excluding all credential-like
        # names by construction.  No arbitrary environment key is archived.
        if (
            (key.startswith("MYSQL_") or key.startswith("BUSINESS_"))
            and (key.endswith("_DB") or key.endswith("DATABASE"))
        ):
            result[key] = item
    return result


def _compose_network(config: Mapping[str, Any], project: str) -> str:
    networks = config.get("networks")
    if not isinstance(networks, Mapping) or set(networks) != {"internal"}:
        _fail("Compose config must contain only the internal network")
    settings = networks["internal"]
    if not isinstance(settings, Mapping) or settings.get("internal") is not True:
        _fail("Compose network is not internal-only")
    if settings.get("external") is True:
        _fail("Compose network cannot be external")
    labels = settings.get("labels")
    if labels is not None and (
        not isinstance(labels, Mapping)
        or labels.get("com.docker.compose.project") not in (None, project)
    ):
        _fail("Compose network project label does not match")
    name = settings.get("name")
    if not isinstance(name, str) or not name:
        _fail("Compose internal network has no concrete name")
    expected = {project + suffix for suffix in ("", "-network", "_network", "-internal", "_internal", "_default")}
    if name not in expected:
        _fail("Compose network is not scoped to the project")
    return name


def _compose_volumes(config: Mapping[str, Any], project: str) -> dict[str, str]:
    raw = config.get("volumes")
    if not isinstance(raw, Mapping) or not raw:
        _fail("Compose config is missing project volumes")
    result: dict[str, str] = {}
    for logical, settings in raw.items():
        logical_name = _required_string(logical, "Compose volume")
        if not isinstance(settings, Mapping):
            _fail("Compose volume definition is malformed")
        if settings.get("external") is True:
            _fail("Compose volume cannot be external")
        actual = settings.get("name") or f"{project}_{logical_name}"
        if not isinstance(actual, str) or not actual.startswith(project + "_"):
            _fail("Compose volume is outside the project")
        result[logical_name] = actual
    return result


def _service_networks(settings: Mapping[str, Any], service: str) -> None:
    networks = settings.get("networks")
    if isinstance(networks, Mapping):
        names = set(networks)
    elif isinstance(networks, list):
        names = set(str(item) for item in networks)
    else:
        _fail(f"Compose service {service} has no network")
    if names != {"internal"}:
        _fail(f"Compose service {service} has an unexpected network")


def _service_has_published_ports(settings: Mapping[str, Any]) -> bool:
    ports = settings.get("ports")
    if ports in (None, [], {}):
        return False
    if not isinstance(ports, list):
        _fail("Compose service ports are malformed")
    for port in ports:
        if isinstance(port, Mapping):
            if port.get("published") not in (None, "", 0):
                return True
            if port.get("host_ip") or port.get("host_ip_range"):
                return True
        else:
            return True
    return False


def _database_value(
    environments: Mapping[str, Mapping[str, str]],
    keys: Sequence[str],
    role: str,
) -> str:
    values = {
        env[key].strip()
        for env in environments.values()
        for key in keys
        if key in env and env[key].strip()
    }
    if len(values) != 1:
        _fail(f"Compose config must identify exactly one {role} database")
    value = next(iter(values))
    if DATABASE_RE.fullmatch(value) is None:
        _fail(f"Compose {role} database is not KShadow scoped")
    return value


def parse_compose_config(
    config: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> ComposeIdentity:
    """Extract and validate non-secret identity data from Compose JSON."""

    project = _required_string(manifest.get("project"), "manifest project")
    manifest_services = _manifest_services(manifest)
    manifest_volumes = _manifest_volumes(manifest, project)
    if config.get("name") is not None and config.get("name") != project:
        _fail("Compose project name does not match the manifest")
    services_raw = config.get("services")
    if not isinstance(services_raw, Mapping) or set(services_raw) != set(manifest_services):
        _fail("Compose services do not match the deployment manifest")
    environments: dict[str, dict[str, str]] = {}
    for service, value in services_raw.items():
        if not isinstance(value, Mapping):
            _fail(f"Compose service {service} is malformed")
        image = value.get("image")
        if not isinstance(image, str) or IMAGE_RE.fullmatch(image) is None:
            _fail(f"Compose service {service} image is not digest pinned")
        if image != manifest_services[service]:
            _fail(f"Compose service {service} image does not match the manifest")
        _service_networks(value, str(service))
        if _service_has_published_ports(value):
            _fail(f"Compose service {service} publishes a host port")
        environments[str(service)] = _env_mapping(value.get("environment"), str(service))
    network = _compose_network(config, project)
    volumes = _compose_volumes(config, project)
    if volumes != manifest_volumes:
        _fail("Compose volumes do not match the deployment manifest")
    databases = {
        "online": _database_value(
            environments,
            ("BUSINESS_ONLINE_DATABASE", "MYSQL_ONLINE_DATA_DB", "MYSQL_DATABASE"),
            "online",
        ),
        "archive": _database_value(
            environments,
            ("BUSINESS_ARCHIVE_DATABASE", "MYSQL_ARCHIVE_DB"),
            "archive",
        ),
        "daily": _database_value(
            environments,
            ("BUSINESS_DAILY_DATABASE", "MYSQL_DAILY_REPORT_DB"),
            "daily",
        ),
    }
    if len(set(databases.values())) != 3:
        _fail("Compose database names must be distinct")
    return ComposeIdentity(
        project=project,
        network=network,
        services=dict(manifest_services),
        volumes=dict(volumes),
        databases=databases,
        environment=environments,
    )


def _labels(record: Mapping[str, Any]) -> Mapping[str, Any]:
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else record.get("Labels")
    if not isinstance(labels, Mapping):
        _fail("container inspect is missing labels")
    return labels


def _service_from_record(record: Mapping[str, Any]) -> str:
    labels = _labels(record)
    service = labels.get("com.docker.compose.service")
    if not isinstance(service, str) or not service:
        _fail("container inspect is missing its Compose service")
    return service


def _sanitize_mounts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _fail("container inspect is missing mounts")
    allowed = ("Type", "Name", "Source", "Destination", "RW", "External")
    result: list[dict[str, Any]] = []
    for mount in value:
        if not isinstance(mount, Mapping):
            _fail("container mount inspection is malformed")
        result.append({key: mount[key] for key in allowed if key in mount})
    return result


def _sanitize_network_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("container inspect is missing network settings")
    networks = value.get("Networks")
    if not isinstance(networks, Mapping):
        _fail("container inspect is missing attached networks")
    ports = value.get("Ports", {})
    if ports is None:
        ports = {}
    if not isinstance(ports, Mapping):
        _fail("container port inspection is malformed")
    return {
        "Networks": {str(name): {} for name in networks},
        "Ports": json.loads(json.dumps(ports)),
    }


def _sanitize_host_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("container inspect is missing host configuration")
    bindings = value.get("PortBindings", {})
    if bindings is None:
        bindings = {}
    if not isinstance(bindings, Mapping):
        _fail("container port binding inspection is malformed")
    return {"PortBindings": json.loads(json.dumps(bindings))}


def _sanitize_container(record: Mapping[str, Any], root: str) -> dict[str, Any]:
    service = _service_from_record(record)
    config = record.get("Config")
    if not isinstance(config, Mapping):
        _fail(f"container {service} is missing Config")
    image = config.get("Image")
    if not isinstance(image, str):
        _fail(f"container {service} is missing Config.Image")
    state = record.get("State")
    if not isinstance(state, Mapping) or state.get("Running") is not True:
        _fail(f"container {service} is not running")
    name = record.get("Name") or record.get("name")
    if not isinstance(name, str) or not name:
        _fail(f"container {service} is missing its name")
    labels = _labels(record)
    sanitized_labels = {
        key: labels[key]
        for key in IDENTITY_LABEL_KEYS
        if key in labels
    }
    sanitized_config: dict[str, Any] = {
        "Image": image,
        "Labels": sanitized_labels,
        "Env": filter_identity_env(config.get("Env", [])),
    }
    result: dict[str, Any] = {
        "Name": name,
        "Config": sanitized_config,
        "Mounts": _sanitize_mounts(record.get("Mounts", [])),
        "HostConfig": _sanitize_host_config(record.get("HostConfig")),
        "NetworkSettings": _sanitize_network_settings(record.get("NetworkSettings")),
    }
    if "Image" in record:
        result["Image"] = record["Image"]
    if "RepoDigests" in record:
        result["RepoDigests"] = record["RepoDigests"]
    return result


def _sanitize_network(record: Mapping[str, Any]) -> dict[str, Any]:
    name = record.get("Name") or record.get("name")
    if not isinstance(name, str) or not name:
        _fail("network inspect is missing its name")
    labels = record.get("Labels")
    if not isinstance(labels, Mapping):
        _fail("network inspect is missing labels")
    members = record.get("Containers")
    if not isinstance(members, Mapping):
        _fail("network inspect is missing container members")
    sanitized_members: dict[str, dict[str, Any]] = {}
    for key, value in members.items():
        if isinstance(value, Mapping) and isinstance(value.get("Name"), str):
            member_name = value["Name"].lstrip("/")
        elif isinstance(value, Mapping) and isinstance(value.get("name"), str):
            member_name = value["name"].lstrip("/")
        elif isinstance(key, str):
            member_name = key
        else:
            _fail("network member has no name")
        sanitized_members[member_name] = {}
    return {
        "Name": name,
        "Internal": record.get("Internal"),
        "Labels": {
            key: labels[key]
            for key in ("com.docker.compose.project", "com.docker.compose.network")
            if key in labels
        },
        "Containers": sanitized_members,
    }


def parse_marker_output(text: str, database: str) -> list[tuple[str, str, str]]:
    """Parse mysql ``-N -B`` output without retaining arbitrary SQL output."""

    if DATABASE_RE.fullmatch(database) is None:
        _fail("marker query database is not KShadow scoped")
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3 or any(not field for field in fields):
            _fail("_shadow_identity query returned malformed data")
        rows.append((fields[0], fields[1], fields[2]))
    if not rows:
        _fail(f"{database} returned no _shadow_identity marker")
    return rows


def build_marker_query_command(container: str, database: str) -> list[str]:
    if not isinstance(container, str) or not container:
        _fail("MySQL container name is missing")
    if DATABASE_RE.fullmatch(database) is None:
        _fail("marker query database is not KShadow scoped")
    sql = "SELECT environment,run_id,database_name FROM _shadow_identity ORDER BY environment,run_id,database_name"
    inner = (
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot -N -B '
        f"--batch {shlex.quote(database)} -e {shlex.quote(sql)}"
    )
    return ["docker", "exec", container, "sh", "-c", inner]


def build_compose_command(
    root: Path,
    env_path: Path,
    compose_files: Sequence[Path],
    project: str,
) -> list[str]:
    args = ["docker", "compose", "--env-file", str(env_path)]
    for compose_file in compose_files:
        args.extend(("-f", str(compose_file)))
    args.extend(("-p", project, "config", "--format", "json"))
    return args


def _db_identity(run_id: str, project: str, databases: Mapping[str, str], rows: Mapping[str, Sequence[Sequence[str]]]) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    for role in ("online", "archive", "daily"):
        name = databases[role]
        role_rows = rows.get(name)
        if role_rows is None:
            _fail(f"missing marker query for {name}")
        markers = [
            {
                "environment": row[0],
                "run_id": row[1],
                "database_name": row[2],
            }
            for row in role_rows
            if len(row) == 3
        ]
        if len(markers) != len(role_rows):
            _fail(f"malformed marker query for {name}")
        result.append({"name": name, "markers": markers})
    return {"run_id": run_id, "project": project, "databases": result}


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_runtime_snapshot(
    manifest: Mapping[str, Any],
    compose_config: Mapping[str, Any],
    container_records: Sequence[Mapping[str, Any]],
    network_record: Mapping[str, Any],
    marker_rows: Mapping[str, Sequence[Sequence[str]]],
    *,
    phase: str = "preflight",
    collected_at: str | None = None,
    root: str | None = None,
) -> dict[str, Any]:
    """Build a sanitized artifact and run the pure business guard on it."""

    manifest_root = _required_string(manifest.get("root"), "manifest root")
    checked_root = validate_current_root(root or manifest_root, root or manifest_root)
    project, run_id, services, volumes = validate_manifest(manifest, checked_root)
    compose = parse_compose_config(compose_config, manifest)
    if compose.project != project:
        _fail("Compose project does not match manifest")
    if compose.services != services or compose.volumes != volumes:
        _fail("Compose identity does not match manifest")
    sanitized_containers = [
        _sanitize_container(record, checked_root)
        for record in container_records
    ]
    sanitized_network = _sanitize_network(network_record)
    db_identity = _db_identity(run_id, project, compose.databases, marker_rows)
    docker_identity: dict[str, Any] = {
        "run_id": run_id,
        "project": project,
        "root": checked_root,
        "network": compose.network,
        "databases": compose.databases,
        "services": services,
        "volumes": volumes,
        "containers": sanitized_containers,
        "network_inspect": sanitized_network,
    }
    identity = validate_saved_identities(run_id, docker_identity, db_identity)
    body: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed",
        "phase": phase,
        "collected_at": collected_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_id": identity.run_id,
        "project": identity.project,
        "root": identity.root,
        "docker_identity": docker_identity,
        "db_identity": db_identity,
    }
    body["sha256"] = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    return body


def _read_json_file(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeInspectionError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        _fail(f"{label} must be a JSON object")
    return value


def _compose_files(root: Path) -> list[Path]:
    files = [root / name for name in COMPOSE_FILENAMES if (root / name).is_file()]
    if not files or files[0].name != "docker-compose.yml":
        _fail("shadow root is missing docker-compose.yml")
    if not (root / "docker-compose.business.yml").is_file():
        _fail("shadow root is missing docker-compose.business.yml")
    return files


def _parse_ps_names(text: str) -> list[str]:
    rows = _parse_json_lines(text, "docker ps")
    names: list[str] = []
    for row in rows:
        name = row.get("Names") or row.get("Name") or row.get("name")
        if not isinstance(name, str) or not name or "," in name:
            _fail("docker ps returned an invalid container name")
        names.append(name.lstrip("/"))
    if len(set(names)) != len(names):
        _fail("docker ps returned duplicate containers")
    return names


def _parse_network_names(text: str) -> list[str]:
    rows = _parse_json_lines(text, "docker network ls")
    names: list[str] = []
    for row in rows:
        name = row.get("Name") or row.get("name")
        if not isinstance(name, str) or not name:
            _fail("docker network ls returned an invalid network name")
        names.append(name)
    return names


def _ensure_inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeInspectionError(f"{label} must be inside the shadow root") from exc
    return resolved


def write_snapshot(path: Path, snapshot: Mapping[str, Any], root: Path) -> Path:
    target = _ensure_inside(path, root / "artifacts", "snapshot output")
    if target.suffix.casefold() != ".json":
        _fail("snapshot output must be a JSON file")
    if target.exists():
        _fail("snapshot output already exists")
    try:
        target.write_text(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise RuntimeInspectionError("snapshot output could not be written") from exc
    return target


def collect_runtime(
    root: str | Path | None = None,
    *,
    run_id: str | None = None,
    manifest_path: str | Path | None = None,
    output_path: str | Path | None = None,
    phase: str = "preflight",
    timeout: float = DEFAULT_TIMEOUT,
    runner: Runner = default_runner,
) -> dict[str, Any]:
    """Collect one fresh live snapshot using read-only commands only."""

    current = Path.cwd().resolve()
    requested = current if root is None else Path(root).resolve()
    root_text = validate_current_root(requested.as_posix(), current.as_posix())
    env_path = requested / ".env"
    if not env_path.is_file():
        _fail("shadow root is missing .env")
    env_identity = parse_dotenv_identity(env_path.read_text(encoding="utf-8"))
    manifest_file = (
        requested / "artifacts" / "deployment-identity.json"
        if manifest_path is None
        else _ensure_inside(Path(manifest_path), requested, "deployment manifest")
    )
    manifest = _read_json_file(manifest_file, "deployment manifest")
    project, expected_run, services, volumes = validate_manifest(manifest, root_text, run_id)
    env_project = _required_env_alias(
        env_identity,
        ("COMPOSE_PROJECT_NAME", "KAFKA_PROJECT"),
        "COMPOSE_PROJECT_NAME or KAFKA_PROJECT",
    )
    if env_project != project:
        _fail(".env project does not match the deployment manifest")
    env_run = _required_env_alias(
        env_identity,
        ("KAFKA_RUN_ID", "LOAD_TEST_RUN_ID"),
        "KAFKA_RUN_ID or LOAD_TEST_RUN_ID",
    )
    if env_run != expected_run:
        _fail(".env run_id does not match the deployment manifest")
    compose_files = _compose_files(requested)
    compose_output = _execute(
        runner,
        build_compose_command(requested, env_path, compose_files, project),
        cwd=requested,
        timeout=timeout,
        phase="compose config",
    )
    compose_config = _parse_json(compose_output, "compose config")
    if not isinstance(compose_config, Mapping):
        _fail("compose config must be a JSON object")
    compose = parse_compose_config(compose_config, manifest)
    env_network = env_identity.get("KAFKA_NETWORK")
    if env_network is not None and env_network != compose.network:
        _fail(".env network does not match the Compose internal network")
    ps_output = _execute(
        runner,
        [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{json .}}",
        ],
        cwd=requested,
        timeout=timeout,
        phase="docker ps",
    )
    names = _parse_ps_names(ps_output)
    inspect_output = _execute(
        runner,
        ["docker", "inspect", *names],
        cwd=requested,
        timeout=timeout,
        phase="docker inspect",
    )
    inspected = _parse_json(inspect_output, "docker inspect")
    if not isinstance(inspected, list) or not all(isinstance(item, Mapping) for item in inspected):
        _fail("docker inspect returned malformed records")
    inspected_names = {
        str(item.get("Name") or item.get("name") or "").lstrip("/")
        for item in inspected
    }
    if inspected_names != set(names):
        _fail("docker inspect container set does not match docker ps")
    network_ls_output = _execute(
        runner,
        [
            "docker",
            "network",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{json .}}",
        ],
        cwd=requested,
        timeout=timeout,
        phase="docker network ls",
    )
    network_names = _parse_network_names(network_ls_output)
    if len(network_names) != 1 or set(network_names) != {compose.network}:
        _fail("Docker project networks do not match the Compose internal network")
    network_output = _execute(
        runner,
        ["docker", "network", "inspect", compose.network],
        cwd=requested,
        timeout=timeout,
        phase="docker network inspect",
    )
    networks = _parse_json(network_output, "docker network inspect")
    if not isinstance(networks, list) or len(networks) != 1 or not isinstance(networks[0], Mapping):
        _fail("docker network inspect returned an unexpected network set")
    mysql_records = [
        item
        for item in inspected
        if _service_from_record(item) == "derived-mysql"
    ]
    if len(mysql_records) != 1:
        _fail("exactly one derived-mysql container is required")
    mysql_name = str(mysql_records[0].get("Name") or "").lstrip("/")
    marker_rows: dict[str, list[tuple[str, str, str]]] = {}
    for database in compose.databases.values():
        marker_output = _execute(
            runner,
            build_marker_query_command(mysql_name, database),
            cwd=requested,
            timeout=timeout,
            phase=f"database marker {database}",
        )
        marker_rows[database] = parse_marker_output(marker_output, database)
    snapshot = build_runtime_snapshot(
        manifest,
        compose_config,
        inspected,
        networks[0],
        marker_rows,
        phase=phase,
        root=root_text,
    )
    snapshot["env_identity"] = env_identity
    snapshot["sha256"] = hashlib.sha256(
        _canonical_bytes({key: value for key, value in snapshot.items() if key != "sha256"})
    ).hexdigest()
    if output_path is None:
        stamp = snapshot["collected_at"].replace(":", "").replace("-", "").replace("Z", "")
        output = requested / "artifacts" / f"business-runtime-{expected_run}-{stamp}.json"
    else:
        output = Path(output_path)
    write_snapshot(output, snapshot, requested)
    return snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--phase",
        choices=("preflight", "seed", "run", "verify", "cleanup"),
        default="preflight",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        snapshot = collect_runtime(
            args.root,
            run_id=args.run_id,
            manifest_path=args.manifest,
            output_path=args.output,
            phase=args.phase,
            timeout=args.timeout,
        )
    except (RuntimeInspectionError, BusinessGuardError) as exc:
        print(f"business runtime inspection failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
