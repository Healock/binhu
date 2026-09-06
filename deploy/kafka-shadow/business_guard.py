#!/usr/bin/env python3
"""Fail-closed preflight checks for the isolated Kafka business shadow.

The guard consumes identity snapshots that were produced by an already
completed, separately audited Docker/DB inspection.  The caller must collect
fresh snapshots immediately before each seed, run, verify, and cleanup phase;
an old artifact cannot authorize a later phase by itself.  This module never
invokes Docker, SSH, MySQL, Compose, or a cleanup command.  That keeps the
preflight pure and makes it possible to review the exact evidence before a
caller starts a synthetic workload.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


RUN_ID_RE = re.compile(r"^KSHADOW-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROJECT_RE = re.compile(r"^binhu-kafka-shadow-[A-Za-z0-9][A-Za-z0-9_.-]*$")
ROOT_RE = re.compile(r"^/srv/binhu-eventbus-shadow-[A-Za-z0-9][A-Za-z0-9_.-]*$")
NETWORK_RE = re.compile(
    r"^binhu-kafka-shadow-[A-Za-z0-9][A-Za-z0-9_.-]*(?:[-_]network)?$"
)
DATABASE_RE = re.compile(r"^KShadow_[A-Za-z0-9_]{1,50}$")
IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# The guard intentionally uses exact namespace boundaries.  Broad substring
# bans would reject a legitimate service such as ``producer`` or a fixture
# directory named ``load-tests`` inside this isolated root.
OLD_LOADTEST_RE = re.compile(r"(?:^|[/\\])binhu-loadtest(?:[-_]|$)")


class BusinessGuardError(ValueError):
    """Raised when a saved identity cannot prove a safe shadow boundary."""


@dataclass(frozen=True)
class BusinessIdentity:
    run_id: str
    project: str
    root: str
    network: str
    databases: dict[str, str]
    services: dict[str, str]


def _fail(message: str) -> None:
    raise BusinessGuardError(message)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} is missing or not a non-empty string")
    result = value.strip()
    _reject_forbidden(result, label)
    return result


def _reject_forbidden(value: str, label: str) -> None:
    folded = value.casefold().replace("\\", "/")
    if (
        folded == "/root/binhu"
        or folded.startswith("/root/binhu/")
        or folded == "/srv/binhu"
        or folded.startswith("/srv/binhu/")
    ):
        _fail(f"production root is forbidden in {label}")
    if OLD_LOADTEST_RE.search(folded):
        _fail(f"old binhu-loadtest identity is forbidden in {label}")


def validate_run_id(value: Any) -> str:
    run_id = _required_string(value, "run_id")
    if RUN_ID_RE.fullmatch(run_id) is None:
        _fail("run_id must use KSHADOW-<isolated identifier> format")
    return run_id


def validate_project(value: Any) -> str:
    project = _required_string(value, "project")
    if PROJECT_RE.fullmatch(project) is None:
        _fail("project must use the binhu-kafka-shadow-* namespace")
    return project


def validate_root(value: Any) -> str:
    root = _required_string(value, "root")
    # Pure POSIX checks are intentional: this is a Linux deployment path even
    # when the snapshots are reviewed on Windows.
    if ROOT_RE.fullmatch(root) is None or "\\" in root:
        _fail("root must be a single /srv/binhu-eventbus-shadow-* directory")
    return root


def validate_network(value: Any, project: str) -> str:
    network = _required_string(value, "network")
    if NETWORK_RE.fullmatch(network) is None:
        _fail("network is not a scoped binhu-kafka-shadow network")
    allowed = {project + suffix for suffix in ("", "-network", "_network", "-internal", "_internal", "_default")}
    if network not in allowed:
        _fail("network is not scoped to the requested project")
    return network


def _aliases(mapping: Mapping[str, Any], keys: Sequence[str], label: str) -> Any:
    present = [mapping[key] for key in keys if key in mapping]
    if not present:
        _fail(f"{label} is missing")
    if any(item != present[0] for item in present[1:]):
        _fail(f"{label} aliases disagree")
    return present[0]


def _labels(record: Mapping[str, Any]) -> Mapping[str, Any]:
    config = record.get("Config")
    if isinstance(config, Mapping) and isinstance(config.get("Labels"), Mapping):
        return config["Labels"]  # type: ignore[return-value]
    labels = record.get("Labels")
    if isinstance(labels, Mapping):
        return labels
    _fail("container is missing Docker labels")


def _service_name(record: Mapping[str, Any]) -> str:
    labels = _labels(record)
    value = record.get("Service") or record.get("service") or labels.get(
        "com.docker.compose.service"
    )
    return _required_string(value, "container service")


def _container_image(record: Mapping[str, Any]) -> tuple[str, list[str]]:
    config = record.get("Config")
    config_image = config.get("Image") if isinstance(config, Mapping) else None
    if not isinstance(config_image, str) or IMAGE_RE.fullmatch(config_image) is None:
        _fail("container Config.Image is not pinned to a sha256 digest")
    image = record.get("Image")
    if image is not None and (
        not isinstance(image, str)
        or (IMAGE_RE.fullmatch(image) is None and IMAGE_ID_RE.fullmatch(image) is None)
    ):
        _fail("container top-level Image is neither a digest reference nor an image id")
    repo_digests = record.get("RepoDigests")
    if not isinstance(repo_digests, list):
        repo_digests = config.get("RepoDigests") if isinstance(config, Mapping) else []
    if not isinstance(repo_digests, list):
        repo_digests = []
    values = [config_image]
    for item in repo_digests:
        if not isinstance(item, str) or IMAGE_RE.fullmatch(item) is None:
            _fail("container RepoDigests contains an invalid digest reference")
        values.append(item)
    if isinstance(image, str) and IMAGE_RE.fullmatch(image):
        values.append(image)
    return config_image, values


def _service_images(identity: Mapping[str, Any]) -> dict[str, str]:
    raw = identity.get("services")
    if raw is None:
        raw = identity.get("images")
    if not isinstance(raw, Mapping) or not raw:
        _fail("Docker identity is missing service image digests")
    result: dict[str, str] = {}
    for service, item in raw.items():
        name = _required_string(service, "service name")
        if isinstance(item, Mapping):
            item = item.get("image")
        image = _required_string(item, f"image for {name}")
        if IMAGE_RE.fullmatch(image) is None:
            _fail(f"image for {name} is not pinned to a sha256 digest")
        result[name] = image
    return result


def _database_names_from_mapping(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        _fail("database names must be an object")
    result: dict[str, str] = {}
    for role, name in raw.items():
        role_name = _required_string(role, "database role")
        result[role_name] = _required_string(name, f"database {role_name}")
    return result


def _validate_database_names(names: Mapping[str, str]) -> dict[str, str]:
    required_roles = {"online", "archive", "daily"}
    if set(names) != required_roles:
        _fail("exactly online, archive, and daily shadow databases are required")
    values = list(names.values())
    if len(set(values)) != 3:
        _fail("shadow database names must be distinct")
    for name in values:
        if DATABASE_RE.fullmatch(name) is None:
            _fail("all business databases must use the KShadow_* namespace")
        if name.casefold() in {"onlinedata", "registrydata", "daily_report", "platformdata"}:
            _fail("production business database name is forbidden")
    return dict(names)


def _database_role(name: str) -> str:
    folded = name.casefold()
    if folded.endswith("_archive"):
        return "archive"
    if folded.endswith("_daily") or folded.endswith("_daily_report"):
        return "daily"
    return "online"


def _marker_rows(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if "markers" in record:
        markers = record["markers"]
    elif "marker" in record:
        markers = record["marker"]
    elif "_shadow_identity" in record:
        markers = record["_shadow_identity"]
    elif "shadow_identity" in record:
        markers = record["shadow_identity"]
    elif "rows" in record:
        markers = record["rows"]
    else:
        _fail("database is missing _shadow_identity marker rows")
    if isinstance(markers, Mapping):
        markers = [markers]
    if not isinstance(markers, list):
        _fail("database marker rows are malformed")
    normalized: list[Mapping[str, Any]] = []
    for item in markers:
        if isinstance(item, Mapping):
            normalized.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 3:
            normalized.append(
                {
                    "environment": item[0],
                    "run_id": item[1],
                    "database_name": item[2],
                }
            )
        else:
            _fail("database marker rows are malformed")
    return normalized


def _database_records(identity: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    raw = identity.get("databases")
    if raw is None:
        raw = identity.get("markers")
    records: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, Mapping):
                name = value.get("name") or value.get("database") or value.get("database_name") or key
                if {"environment", "run_id", "database_name"} <= set(value):
                    record = {"markers": [value]}
                else:
                    record = value
            elif isinstance(value, list):
                name = key
                record = {"markers": value}
            else:
                _fail("database identity record is malformed")
            records.append((_required_string(name, "database name"), record))
    elif isinstance(raw, list):
        for value in raw:
            if not isinstance(value, Mapping):
                _fail("database identity record is malformed")
            name = value.get("name") or value.get("database") or value.get("database_name")
            if {"environment", "run_id", "database_name"} <= set(value):
                value = {"markers": [value], "name": name}
            records.append((_required_string(name, "database name"), value))
    else:
        _fail("database identity is missing databases")
    if len(records) != 3 or len({name for name, _ in records}) != 3:
        _fail("exactly three shadow databases are required")
    return records


def _validate_db_identity(
    run_id: str,
    project: str,
    expected: Mapping[str, str] | None,
    identity: Mapping[str, Any],
) -> dict[str, str]:
    identity_run = identity.get("run_id")
    if identity_run is not None and validate_run_id(identity_run) != run_id:
        _fail("database identity run_id does not match")
    identity_project = identity.get("project")
    if identity_project is not None and validate_project(identity_project) != project:
        _fail("database identity project does not match")
    records = _database_records(identity)
    names = [name for name, _ in records]
    for name in names:
        if DATABASE_RE.fullmatch(name) is None:
            _fail("database identity contains a non-shadow database")
    if expected is not None:
        if set(expected.values()) != set(names):
            _fail("database names do not match the Docker identity")
        by_role = dict(expected)
    else:
        roles = [_database_role(name) for name in names]
        if set(roles) != {"online", "archive", "daily"} or len(set(roles)) != 3:
            _fail("database names must identify one online, archive, and daily database")
        by_role = {role: name for role, name in zip(roles, names)}
    for name, record in records:
        markers = _marker_rows(record)
        if len(markers) != 1:
            _fail(f"database {name} must contain exactly one shadow marker")
        marker = markers[0]
        if marker.get("environment") != "shadow":
            _fail(f"database {name} marker environment is not shadow")
        if marker.get("run_id") != run_id:
            _fail(f"database {name} marker run_id does not match")
        if marker.get("database_name") != name:
            _fail(f"database {name} marker database_name does not match")
        table = marker.get("table")
        if table is not None and table != "_shadow_identity":
            _fail(f"database {name} marker table is not _shadow_identity")
    return by_role


def _identity_network(identity: Mapping[str, Any], project: str) -> tuple[str, Mapping[str, Any]]:
    inspect = identity.get("network_inspect")
    if inspect is None:
        inspect = identity.get("network")
    network_name = identity.get("network_name")
    if isinstance(inspect, list):
        if len(inspect) != 1 or not isinstance(inspect[0], Mapping):
            _fail("network inspect must contain exactly one network")
        inspect = inspect[0]
    if isinstance(inspect, Mapping):
        network_name = inspect.get("Name") or inspect.get("name") or network_name
    if network_name is None:
        network_name = f"{project}-network"
    network = validate_network(network_name, project)
    if not isinstance(inspect, Mapping):
        _fail("Docker identity is missing internal network inspection")
    if inspect.get("Internal") is not True:
        _fail("network is not internal-only")
    labels = inspect.get("Labels")
    if not isinstance(labels, Mapping):
        _fail("network inspection is missing labels")
    if labels.get("com.docker.compose.project") != project:
        _fail("network compose project label does not match")
    if labels.get("com.docker.compose.network") != "internal":
        _fail("network is not the Compose internal network")
    if str(inspect.get("Name") or inspect.get("name") or network) != network:
        _fail("network name does not match")
    return network, inspect


def _env_values(record: Mapping[str, Any]) -> dict[str, str]:
    config = record.get("Config")
    raw = config.get("Env") if isinstance(config, Mapping) else record.get("Env")
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items()}
    if not isinstance(raw, list):
        _fail("container environment is malformed")
    values: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, str) or "=" not in item:
            _fail("container environment entry is malformed")
        key, value = item.split("=", 1)
        values[key] = value
    return values


def _under_root(source: str, root: str) -> bool:
    if "\\" in source:
        return False
    normalized_source = posixpath.normpath(source)
    normalized_root = posixpath.normpath(root)
    return normalized_source == normalized_root or normalized_source.startswith(normalized_root + "/")


def _validate_mounts(
    record: Mapping[str, Any],
    project: str,
    root: str,
    declared_volume_names: set[str],
) -> None:
    mounts = record.get("Mounts")
    if not isinstance(mounts, list):
        _fail("container is missing real mount inspection")
    for mount in mounts:
        if not isinstance(mount, Mapping):
            _fail("container mount record is malformed")
        mount_type = mount.get("Type")
        if mount_type == "bind":
            source = mount.get("Source")
            if not isinstance(source, str) or not _under_root(source, root):
                _fail("bind mount is outside the isolated shadow root")
        elif mount_type == "volume":
            name = mount.get("Name")
            if not isinstance(name, str) or not name:
                _fail("volume mount is missing its Docker volume name")
            if mount.get("External") is True:
                _fail("external Docker volume is forbidden")
            if declared_volume_names:
                if name not in declared_volume_names:
                    _fail("volume mount is not declared by this project")
            elif not name.startswith(project + "_"):
                _fail("volume mount is outside the Compose project")
        elif mount_type == "tmpfs":
            if mount.get("Name") or mount.get("Source"):
                _fail("tmpfs mount contains an external source")
        else:
            _fail("container has an unsupported external mount type")


def _validate_no_ports(record: Mapping[str, Any]) -> None:
    network_settings = record.get("NetworkSettings")
    if not isinstance(network_settings, Mapping):
        _fail("container is missing NetworkSettings")
    ports = network_settings.get("Ports")
    if isinstance(ports, Mapping):
        if any(value not in (None, [], {}) for value in ports.values()):
            _fail("container publishes a host port")
    elif ports not in (None, [], {}):
        _fail("container port inspection is malformed")
    top_level_ports = record.get("Ports")
    if isinstance(top_level_ports, Mapping):
        if any(value not in (None, [], {}) for value in top_level_ports.values()):
            _fail("container publishes a host port")
    elif top_level_ports not in (None, [], {}):
        _fail("container port inspection is malformed")
    host_config = record.get("HostConfig")
    if isinstance(host_config, Mapping):
        bindings = host_config.get("PortBindings")
        if isinstance(bindings, Mapping) and any(value not in (None, [], {}) for value in bindings.values()):
            _fail("container has a host port binding")


def _validate_containers(
    identity: Mapping[str, Any],
    *,
    run_id: str,
    project: str,
    root: str,
    network: str,
    services: Mapping[str, str],
    database_names: set[str],
    declared_volume_names: set[str],
    seen_volume_names: set[str],
) -> None:
    containers = identity.get("containers")
    if isinstance(containers, Mapping):
        containers = list(containers.values())
    if not isinstance(containers, list) or not containers:
        _fail("Docker identity is missing real container inspection")
    seen_services: set[str] = set()
    seen_names: set[str] = set()
    for record in containers:
        if not isinstance(record, Mapping):
            _fail("container inspection record is malformed")
        service = _service_name(record)
        if service in seen_services:
            _fail("Docker identity contains duplicate services")
        seen_services.add(service)
        if service not in services:
            _fail(f"container service {service} is not declared by this run")
        name = record.get("Name") or record.get("name")
        name = _required_string(name, f"container name for {service}").lstrip("/")
        if name not in {f"{project}-{service}", f"{project}-{service}-1"}:
            _fail(f"container {service} is outside the isolated project")
        if name in seen_names:
            _fail("Docker identity contains duplicate container names")
        seen_names.add(name)
        labels = _labels(record)
        expected_labels = {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
            "com.docker.compose.project.working_dir": root,
            "binhu.shadow": "true",
            "binhu.shadow.run_id": run_id,
            "binhu.shadow.scope": "business",
        }
        for key, expected in expected_labels.items():
            if labels.get(key) != expected:
                _fail(f"container {service} label {key} does not match")
        config_image, image_values = _container_image(record)
        expected_image = services[service]
        if config_image != expected_image:
            _fail(f"container {service} Config.Image digest does not match identity")
        repo_digests = record.get("RepoDigests")
        if not isinstance(repo_digests, list):
            config = record.get("Config")
            repo_digests = config.get("RepoDigests") if isinstance(config, Mapping) else []
        if repo_digests and expected_image not in image_values:
            _fail(f"container {service} RepoDigest does not match identity")
        environment = _env_values(record)
        for key in ("KAFKA_RUN_ID", "LOAD_TEST_RUN_ID"):
            if key in environment and environment[key] != run_id:
                _fail(f"container {service} environment run_id does not match")
        if environment.get("APP_ENVIRONMENT") not in (None, "shadow"):
            _fail(f"container {service} is not in shadow environment")
        for key, value in environment.items():
            if (
                value
                and "PASSWORD" not in key
                and "TOKEN" not in key
                and "SECRET" not in key
                and "KEY" not in key
                and (
                    key in {"APP_ENVIRONMENT", "COMPOSE_PROJECT_NAME", "KAFKA_RUN_ID", "LOAD_TEST_RUN_ID", "MYSQL_HOST"}
                    or key.endswith("DATABASE")
                    or key.endswith("_DB")
                )
            ):
                _reject_forbidden(value, f"container {service} environment {key}")
            if key.endswith("DATABASE") or key.endswith("_DB"):
                if value and value not in database_names:
                    _fail(f"container {service} points to an unexpected database")
        network_settings = record.get("NetworkSettings")
        attached = network_settings.get("Networks") if isinstance(network_settings, Mapping) else None
        if not isinstance(attached, Mapping) or set(attached) != {network}:
            _fail(f"container {service} is attached to an unexpected network")
        _validate_no_ports(record)
        _validate_mounts(record, project, root, declared_volume_names)
        for mount in record["Mounts"]:
            if isinstance(mount, Mapping) and mount.get("Type") == "volume":
                seen_volume_names.add(str(mount.get("Name")))
    if set(services) != seen_services:
        _fail("declared services and inspected containers do not match")

    network_inspect = identity.get("network_inspect")
    if network_inspect is None and isinstance(identity.get("network"), Mapping):
        network_inspect = identity["network"]
    if isinstance(network_inspect, list):
        network_inspect = network_inspect[0] if len(network_inspect) == 1 else None
    if not isinstance(network_inspect, Mapping):
        _fail("network inspection is missing")
    members = network_inspect.get("Containers")
    if not isinstance(members, Mapping):
        _fail("internal network contains an unexpected container set")
    member_names: set[str] = set()
    for key, value in members.items():
        if key in seen_names:
            member_names.add(str(key))
        elif isinstance(value, Mapping) and isinstance(value.get("Name"), str):
            member_names.add(value["Name"].lstrip("/"))
        elif isinstance(value, Mapping) and isinstance(value.get("name"), str):
            member_names.add(value["name"].lstrip("/"))
        else:
            _fail("internal network contains an unnamed container")
    if member_names != seen_names:
        _fail("internal network contains an unexpected container set")


def validate_saved_identities(
    run_id: Any,
    docker_identity: Mapping[str, Any],
    db_identity: Mapping[str, Any],
) -> BusinessIdentity:
    """Validate two saved identity snapshots without side effects."""

    if not isinstance(docker_identity, Mapping) or not isinstance(db_identity, Mapping):
        _fail("identity snapshots must be JSON objects")
    expected_run = validate_run_id(run_id)
    snapshot_run = _aliases(docker_identity, ("run_id", "KAFKA_RUN_ID"), "run_id")
    if validate_run_id(snapshot_run) != expected_run:
        _fail("Docker identity run_id does not match")
    db_snapshot_run = db_identity.get("run_id")
    if db_snapshot_run is not None and validate_run_id(db_snapshot_run) != expected_run:
        _fail("database identity run_id does not match")

    project = validate_project(_aliases(docker_identity, ("project", "compose_project"), "project"))
    root = validate_root(_aliases(docker_identity, ("root", "project_root", "project_dir"), "root"))
    network, _ = _identity_network(docker_identity, project)
    services = _service_images(docker_identity)

    expected_databases = None
    if "databases" in docker_identity and isinstance(docker_identity["databases"], Mapping):
        expected_databases = _validate_database_names(
            _database_names_from_mapping(docker_identity["databases"])
        )
    databases = _validate_db_identity(expected_run, project, expected_databases, db_identity)

    declared_volumes: set[str] = set()
    volumes = docker_identity.get("volumes")
    if volumes is not None:
        if not isinstance(volumes, Mapping):
            _fail("volumes must be a Docker volume mapping")
        for logical, docker_name in volumes.items():
            _required_string(logical, "volume name")
            actual = _required_string(docker_name, f"Docker volume for {logical}")
            if not actual.startswith(project + "_"):
                _fail("declared Docker volume is outside the project")
            declared_volumes.add(actual)
    seen_volumes: set[str] = set()

    status = docker_identity.get("status")
    if status is not None and str(status).casefold() not in {"running", "healthy", "up"}:
        _fail("Docker identity status is not running")
    _validate_containers(
        docker_identity,
        run_id=expected_run,
        project=project,
        root=root,
        network=network,
        services=services,
        database_names=set(databases.values()),
        declared_volume_names=declared_volumes,
        seen_volume_names=seen_volumes,
    )
    if declared_volumes and seen_volumes != declared_volumes:
        _fail("declared Docker volumes and real container mounts do not match")
    if expected_databases is not None and expected_databases != databases:
        _fail("database identities do not match")
    return BusinessIdentity(expected_run, project, root, network, databases, dict(services))


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessGuardError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        _fail(f"{label} must contain a JSON object")
    return value  # type: ignore[return-value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "The caller must collect fresh Docker and DB snapshots before "
            "each seed/run/verify/cleanup phase; this command does not grant "
            "freshness to an old artifact."
        ),
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--docker-identity",
        "--docker-json",
        "--deployment-identity",
        dest="docker_identity",
        required=True,
        type=Path,
    )
    parser.add_argument("--db-identity", "--db-json", dest="db_identity", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        identity = validate_saved_identities(
            args.run_id,
            _load_json(args.docker_identity, "Docker identity"),
            _load_json(args.db_identity, "database identity"),
        )
    except BusinessGuardError as exc:
        print(f"business shadow preflight failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "passed",
                "run_id": identity.run_id,
                "project": identity.project,
                "root": identity.root,
                "network": identity.network,
                "databases": identity.databases,
                "services": identity.services,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
