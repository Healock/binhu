"""Prepare a new Dev pipeline project from explicit images, without starting it."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

from .runtime import configuration
from .services.kafka_delivery_store import SCHEMA_SQL
from .flink_sql import render

PROJECT = "binhu-development-pipeline"
PUBLIC_CANDIDATE_FILES = frozenset({
    "init.sql", "redis.conf", "pipeline.sql", "runtime.py",
    "dual_track_monitor.py", "scale_acceptance.py", "kafka_delivery_store.py",
    "kafka_event_contract.py", "kafka_envelope.py", "kafka_relay.py",
    "delivery_schema_migrate.py",
})
ROOT = Path("/srv/binhu-environments/development-pipeline")
NETWORK = "binhu-development-eventbus_internal"
BACKEND_NETWORK = "binhu-development_internal"
SECRET_RE = re.compile(r"[0-9a-f]{48}\Z")
EXPECTED_SERVICES = frozenset(
    {
        "dev-derived-mysql",
        "dev-derived-redis",
        "relay",
        "bridge",
        "business-bridge",
        "backend-outbox-relay",
        "python-metadata-worker",
        "dual-track-monitor",
        "acceptance-runner",
    }
)
# The relay was introduced after the first trusted Dev project was created.
# Its legacy container can therefore lack the environment label; Compose will
# recreate it from the current definition, which carries the label.
LEGACY_MISSING_ENV_LABEL_SERVICES = frozenset({"backend-outbox-relay"})
_SAFE_PREPARE_FAILURES = frozenset(
    {
        "new absolute Dev output required",
        "existing Dev root has no trusted manifest",
        "existing Dev manifest is unreadable",
        "existing root identity mismatch",
        "existing Dev project identity mismatch",
        "existing Dev volume identity mismatch",
        "isolated Dev eventbus network required",
        "foreign network dependency",
        "insufficient memory reserve",
        "insufficient disk reserve",
        "persistent Dev credential files are incomplete",
        "persistent Dev credential files are invalid",
        "persistent Dev credential mismatch",
        "Dev Backend relay credentials must be supplied out of band",
        "Dev identity required",
        "isolated Dev targets required",
        "independent runtime credential required",
        "external environment Redis is forbidden",
        "image identity mismatch",
    }
)


def safe_prepare_failure_detail(error: Exception) -> str:
    """Return only an allow-listed prepare gate reason for CI diagnostics."""
    detail = str(error)
    return detail if detail in _SAFE_PREPARE_FAILURES else type(error).__name__


def compose(images):
    if set(images) != {"mysql", "redis", "worker", "flink"} or any(
        not re.fullmatch(r"sha256:[0-9a-f]{64}", v or "") for v in images.values()
    ):
        raise ValueError("four immutable image identities required")
    common = {"networks": ["internal"], "labels": {"binhu.environment": "development"},
              "restart": "on-failure:3", "pull_policy": "never", "pids_limit": 128,
              "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}},
              "security_opt": ["no-new-privileges:true"]}
    services = {
        "dev-derived-mysql": {**common, "image": images["mysql"], "env_file": ["mysql.env"],
            # Keep the Dev-only database cap high enough for the 100k controlled
            # enqueue while remaining explicit and bounded for the shared host;
            # the swap cap prevents an unbounded host-level fallback.
            "mem_limit": "768m", "memswap_limit": "1536m", "cpus": .5,
            "healthcheck": {"test": ["CMD", "mysqladmin", "ping", "-h127.0.0.1", "--silent"],
                            "interval": "5s", "timeout": "3s", "retries": 36, "start_period": "180s"},
            "command": ["--innodb-buffer-pool-size=128M", "--max-connections=20",
                        "--innodb-file-per-table=OFF", "--innodb-data-file-path=ibdata1:12M:autoextend:max:1024M",
                        "--innodb-redo-log-capacity=64M", "--skip-log-bin"],
            "volumes": ["mysql:/var/lib/mysql", "./init.sql:/docker-entrypoint-initdb.d/01-pipeline.sql:ro"]},
        "dev-derived-redis": {**common, "image": images["redis"], "mem_limit": "96m", "cpus": .25,
            "command": ["redis-server", "/usr/local/etc/redis/redis.conf"],
            "volumes": ["redis:/data", "./redis.conf:/usr/local/etc/redis/redis.conf:ro"]},
    }
    for mode in ("relay", "bridge"):
        services[mode] = {**common, "image": images["worker"], "env_file": ["runtime.env"],
            "mem_limit": "160m", "cpus": .25, "read_only": True, "tmpfs": ["/tmp:size=16m"],
            "command": ["python", "-m", "event_pipeline.runtime", mode],
            "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
            "depends_on": {"dev-derived-mysql": {"condition": "service_healthy"},
                           "dev-derived-redis": {"condition": "service_started"}}}
    services["relay"].update({
        "mem_limit": "256m", "cpus": 1,
        "volumes": [
            "./runtime.py:/opt/dev-pipeline/event_pipeline/runtime.py:ro",
            "./kafka_delivery_store.py:/opt/dev-pipeline/event_pipeline/services/kafka_delivery_store.py:ro",
            "./kafka_event_contract.py:/opt/dev-pipeline/event_pipeline/services/kafka_event_contract.py:ro",
            "./kafka_envelope.py:/opt/dev-pipeline/event_pipeline/services/kafka_envelope.py:ro",
            "./kafka_relay.py:/opt/dev-pipeline/event_pipeline/services/kafka_relay.py:ro",
            "./delivery_schema_migrate.py:/opt/dev-pipeline/event_pipeline/delivery_schema_migrate.py:ro",
        ],
    })
    services["business-bridge"] = {**common, "image": images["worker"],
        "networks": ["internal", "backend"], "env_file": ["runtime.env", "backend-relay.env"],
        "mem_limit": "160m", "cpus": .25, "read_only": True,
        "tmpfs": ["/tmp:size=16m"],
        "command": ["python", "-m", "event_pipeline.runtime", "business-bridge"],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "depends_on": {"dev-derived-mysql": {"condition": "service_healthy"},
                       "dev-derived-redis": {"condition": "service_started"}}}
    services["backend-outbox-relay"] = {**common, "image": images["worker"],
        "networks": ["backend"], "env_file": ["backend-relay.env"],
        "mem_limit": "160m", "cpus": .25, "read_only": True,
        "tmpfs": ["/tmp:size=16m"],
        "command": ["python", "-m", "event_pipeline.runtime", "backend-outbox-relay"],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "depends_on": {}}
    services["python-metadata-worker"] = {**common, "image": images["worker"],
        "env_file": ["runtime.env"], "mem_limit": "256m", "cpus": .25,
        "read_only": True, "tmpfs": ["/tmp:size=16m"],
        "command": ["python", "-m", "event_pipeline.runtime", "python-metadata-worker"],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "depends_on": {"dev-derived-mysql": {"condition": "service_healthy"}}}
    services["dual-track-monitor"] = {**common, "image": images["worker"],
        "env_file": ["runtime.env"], "mem_limit": "128m", "cpus": .2,
        "read_only": True, "tmpfs": ["/tmp:size=16m"],
        "volumes": ["evidence:/var/lib/binhu-dev-event-pipeline/evidence",
                    "./runtime.py:/opt/dev-pipeline/event_pipeline/runtime.py:ro",
                    "./dual_track_monitor.py:/opt/dev-pipeline/event_pipeline/dual_track_monitor.py:ro"],
        "command": ["python", "-m", "event_pipeline.runtime", "dual-track-monitor"],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "depends_on": {"dev-derived-mysql": {"condition": "service_healthy"}}}
    services["acceptance-runner"] = {**common, "image": images["worker"],
        "profiles": ["acceptance"], "restart": "no", "env_file": ["runtime.env"],
        "mem_limit": "192m", "cpus": .5, "read_only": True,
        "tmpfs": ["/tmp:size=16m"],
        "volumes": ["evidence:/var/lib/binhu-dev-event-pipeline/evidence",
                    "./scale_acceptance.py:/opt/dev-pipeline/event_pipeline/scale_acceptance.py:ro",
                    "./runtime.py:/opt/dev-pipeline/event_pipeline/runtime.py:ro",
                    "./kafka_delivery_store.py:/opt/dev-pipeline/event_pipeline/services/kafka_delivery_store.py:ro",
                    "./kafka_event_contract.py:/opt/dev-pipeline/event_pipeline/services/kafka_event_contract.py:ro",
                    "./kafka_envelope.py:/opt/dev-pipeline/event_pipeline/services/kafka_envelope.py:ro",
                    "./kafka_relay.py:/opt/dev-pipeline/event_pipeline/services/kafka_relay.py:ro"],
        "command": ["python", "-m", "event_pipeline.scale_acceptance"],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "depends_on": {"dev-derived-mysql": {"condition": "service_healthy"}}}
    return {"name": PROJECT, "services": services,
            "networks": {"internal": {"external": True, "name": NETWORK},
                         "backend": {"external": True, "name": BACKEND_NETWORK}},
            "volumes": {name: {"labels": {"binhu.environment": "development"}} for name in ("mysql", "redis", "evidence")}}


def checked(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("Dev resource inspection failed")
    return result.stdout


def validate_existing_container_identity(item):
    """Accept only trusted Dev containers, including one known legacy label gap."""
    labels = item.get("Config", {}).get("Labels", {})
    name = str(item.get("Name", "")).lstrip("/")
    service = labels.get("com.docker.compose.service")
    if (
        labels.get("com.docker.compose.project") != PROJECT
        or not name.startswith(PROJECT + "-")
        or service not in EXPECTED_SERVICES
    ):
        raise ValueError("existing Dev project identity mismatch")
    environment = labels.get("binhu.environment")
    if environment == "development":
        return
    if environment is None and service in LEGACY_MISSING_ENV_LABEL_SERVICES:
        return
    raise ValueError("existing Dev project identity mismatch")


def _read_env_file(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("persistent Dev credential files are incomplete")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("persistent Dev credential files are invalid")
        values[key] = value
    return values


def _load_credentials() -> tuple[str, str, str]:
    """Reuse credentials belonging to retained named volumes.

    The MySQL and Redis accounts live in their persistent volumes.  Generating
    a new env file on every prepare would change the container configuration
    without changing those accounts, so updates must reuse the last trusted
    credentials and fail closed when the private files disagree.
    """
    if not ROOT.exists():
        return tuple(secrets.token_hex(24) for _ in range(3))
    runtime = _read_env_file(ROOT / "runtime.env")
    mysql = _read_env_file(ROOT / "mysql.env")
    redis_path = ROOT / "redis.conf"
    if redis_path.is_symlink() or not redis_path.is_file():
        raise ValueError("persistent Dev credential files are incomplete")
    redis_matches = re.findall(r"(?m)^requirepass ([0-9a-f]{48})\s*$", redis_path.read_text(encoding="utf-8"))
    db_password = runtime.get("MYSQL_PASSWORD", "")
    redis_password = runtime.get("REDIS_PASSWORD", "")
    root_password = mysql.get("MYSQL_ROOT_PASSWORD", "")
    if (
        not SECRET_RE.fullmatch(db_password)
        or not SECRET_RE.fullmatch(redis_password)
        or not SECRET_RE.fullmatch(root_password)
        or mysql.get("MYSQL_PASSWORD") != db_password
        or redis_matches != [redis_password]
    ):
        raise ValueError("persistent Dev credential mismatch")
    return db_password, redis_password, root_password


def preflight():
    if ROOT.is_symlink() or ROOT.parent.is_symlink() or ROOT.resolve() != ROOT:
        raise ValueError("new absolute Dev output required")
    if ROOT.exists():
        manifest_path = ROOT / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("existing Dev root has no trusted manifest")
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("existing Dev manifest is unreadable") from None
        if previous.get("environment") != "development" or previous.get("project") != PROJECT:
            raise ValueError("existing root identity mismatch")
    existing = checked(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]).strip()
    if existing:
        for item in json.loads(checked(["docker", "inspect", *existing.split()])):
            validate_existing_container_identity(item)
    network = json.loads(checked(["docker", "network", "inspect", NETWORK]))[0]
    if not network.get("Internal") or network.get("Labels", {}).get("com.docker.compose.project") != "binhu-development-eventbus":
        raise ValueError("isolated Dev eventbus network required")
    for item in network.get("Containers", {}).values():
        if not item["Name"].startswith("binhu-development-"):
            raise ValueError("foreign network dependency")
    volumes = checked(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines()
    for volume in volumes:
        if volume.startswith(PROJECT + "_"):
            info = json.loads(checked(["docker", "volume", "inspect", volume]))[0]
            labels = info.get("Labels", {})
            if labels.get("com.docker.compose.project") != PROJECT or labels.get("binhu.environment") != "development":
                raise ValueError("existing Dev volume identity mismatch")
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    if int(memory["MemAvailable"].split()[0]) < 3 * 1024**2:
        raise ValueError("insufficient memory reserve")
    for path in (ROOT.parent, Path("/data/docker")):
        stats = os.statvfs(path)
        if stats.f_bavail * stats.f_frsize < 10 * 1024**3:
            raise ValueError("insufficient disk reserve")


def prepare(run_id, images):
    spec = compose(images)
    preflight()
    db_password, redis_password, root_password = _load_credentials()
    env = {"APP_ENVIRONMENT": "development", "DEV_RUN_ID": run_id,
           "MYSQL_HOST": "dev-derived-mysql", "MYSQL_DATABASE": "Dev_EventPipeline",
           "MYSQL_USER": "dev_pipeline", "MYSQL_PASSWORD": db_password,
           "REDIS_HOST": "dev-derived-redis", "REDIS_PASSWORD": redis_password,
           "KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9092,kafka-2:9092,kafka-3:9092",
           "BACKEND_REDIS_URL": os.environ.get("DEV_BACKEND_REDIS_URL", ""),
           "BACKEND_REDIS_STREAM_KEY": "binhu:events",
           "BACKEND_REDIS_START_ID": "$"}
    backend_password = os.environ.get("DEV_BACKEND_MYSQL_PASSWORD", "")
    backend_redis_url = os.environ.get("DEV_BACKEND_REDIS_URL", "")
    # Updates to an already trusted Dev project reuse only that project's
    # private relay credentials. First-time creation still requires the
    # gateway's out-of-band configuration.
    relay_file = ROOT / "backend-relay.env"
    if (not backend_password or not backend_redis_url) and relay_file.is_file() and not relay_file.is_symlink():
        previous = {}
        for line in relay_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                previous[key] = value
        if previous.get("APP_ENVIRONMENT") == "development" and previous.get("BACKEND_MYSQL_HOST") == "environment-mysql" and previous.get("BACKEND_MYSQL_DATABASE") == "Dev_OnlineData":
            backend_password = backend_password or previous.get("BACKEND_MYSQL_PASSWORD", "")
            backend_redis_url = backend_redis_url or previous.get("BACKEND_REDIS_URL", "")
    if not backend_password or not backend_redis_url:
        raise ValueError("Dev Backend relay credentials must be supplied out of band")
    configuration(env)
    for image in images.values():
        if checked(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip() != image:
            raise ValueError("image identity mismatch")
    ROOT.mkdir(mode=0o700, exist_ok=True)
    ROOT.chmod(0o700)
    sql = f"""USE Dev_EventPipeline;
CREATE TABLE _pipeline_identity (id INT PRIMARY KEY, environment VARCHAR(32), run_id VARCHAR(80), database_name VARCHAR(64));
INSERT INTO _pipeline_identity VALUES (1,'development','{run_id}','Dev_EventPipeline');
{SCHEMA_SQL};
CREATE TABLE dev_task_revisions (
 run_id VARCHAR(80) NOT NULL, task_id VARCHAR(96) NOT NULL,
 source_id BIGINT NOT NULL, revision BIGINT NOT NULL,
 PRIMARY KEY (run_id,task_id,source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE dev_task_metadata (
 run_id VARCHAR(80) NOT NULL, task_id VARCHAR(96) NOT NULL,
 source_id BIGINT NOT NULL, revision BIGINT NOT NULL,
 event_count BIGINT NOT NULL, changed_field_count BIGINT NOT NULL,
 created_count BIGINT NOT NULL, saved_count BIGINT NOT NULL,
 claimed_count BIGINT NOT NULL, assigned_count BIGINT NOT NULL,
 reviewed_count BIGINT NOT NULL, archived_count BIGINT NOT NULL,
 deleted_count BIGINT NOT NULL,
 PRIMARY KEY (run_id,task_id,source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE dev_task_metadata_python (
 run_id VARCHAR(80) NOT NULL, task_id VARCHAR(96) NOT NULL,
 source_id BIGINT NOT NULL, revision BIGINT NOT NULL,
 event_count BIGINT NOT NULL, changed_field_count BIGINT NOT NULL,
 created_count BIGINT NOT NULL, saved_count BIGINT NOT NULL,
 claimed_count BIGINT NOT NULL, assigned_count BIGINT NOT NULL,
 reviewed_count BIGINT NOT NULL, archived_count BIGINT NOT NULL,
 deleted_count BIGINT NOT NULL,
 PRIMARY KEY (run_id,task_id,source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE dev_task_metadata_python_events (
 event_id CHAR(36) NOT NULL,
 run_id VARCHAR(80) NOT NULL, task_id VARCHAR(96) NOT NULL,
 source_id BIGINT NOT NULL, revision BIGINT NOT NULL,
 canonical_sha256 CHAR(64) NOT NULL,
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 PRIMARY KEY (run_id,event_id),
 INDEX python_event_task (run_id,task_id,source_id,revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
"""
    services_root = Path(__file__).with_name("services")
    files = {
        "compose.json": json.dumps(spec, indent=2), "init.sql": sql,
        "runtime.env": "\n".join(f"{k}={v}" for k, v in env.items()) + "\n",
        "backend-relay.env": "\n".join([
            "APP_ENVIRONMENT=development", f"DEV_RUN_ID={run_id}",
            "BACKEND_MYSQL_HOST=environment-mysql", "BACKEND_MYSQL_DATABASE=Dev_OnlineData",
            "BACKEND_MYSQL_USER=environment_app", f"BACKEND_MYSQL_PASSWORD={backend_password}",
            f"BACKEND_REDIS_URL={backend_redis_url}", "BACKEND_REDIS_STREAM_KEY=binhu:events",
        ]) + "\n",
        "mysql.env": f"MYSQL_ROOT_PASSWORD={root_password}\nMYSQL_DATABASE=Dev_EventPipeline\nMYSQL_USER=dev_pipeline\nMYSQL_PASSWORD={db_password}\n",
        "redis.conf": f"bind 0.0.0.0\nprotected-mode yes\nrequirepass {redis_password}\nmaxmemory 48mb\nmaxmemory-policy noeviction\nappendonly yes\nappendfsync everysec\nauto-aof-rewrite-percentage 100\nauto-aof-rewrite-min-size 16mb\n",
        "pipeline.sql": render(env),
        # The existing worker digest predates the resident monitor.  Mount the
        # two small dispatcher modules read-only so this service is bound to
        # the candidate source without rebuilding or mutating the worker image.
        # Mount the actual runtime dispatcher into the legacy worker image.
        # ``__file__`` is prepare.py here; reading it would make the mounted
        # runtime module self-import and fail with a circular import.
        "runtime.py": Path(__file__).with_name("runtime.py").read_text(encoding="utf-8"),
        "dual_track_monitor.py": Path(__file__).with_name("dual_track_monitor.py").read_text(encoding="utf-8"),
        "scale_acceptance.py": Path(__file__).with_name("scale_acceptance.py").read_text(encoding="utf-8"),
        "kafka_delivery_store.py": (services_root / "kafka_delivery_store.py").read_text(encoding="utf-8"),
        "kafka_event_contract.py": (services_root / "kafka_event_contract.py").read_text(encoding="utf-8"),
        "kafka_envelope.py": (services_root / "kafka_envelope.py").read_text(encoding="utf-8"),
        "kafka_relay.py": (services_root / "kafka_relay.py").read_text(encoding="utf-8"),
        "delivery_schema_migrate.py": Path(__file__).with_name("delivery_schema_migrate.py").read_text(encoding="utf-8"),
    }
    for name, content in files.items():
        target = ROOT / name
        # A failed candidate extraction can leave a same-named directory at a
        # generated file path. It is not a valid candidate artifact; remove
        # only that exact path before writing the next isolated Dev candidate.
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        target.write_text(content, encoding="utf-8")
        # Parent directory stays private.  Files consumed by an unprivileged
        # container user are explicitly readable; credentials and manifests
        # remain owner-only.
        target.chmod(0o644 if name in PUBLIC_CANDIDATE_FILES else 0o600)
    manifest = {"environment": "development", "project": PROJECT, "run_id": run_id,
                "images": images, "started": False, "acceptance": "pending",
                "hashes": {k: hashlib.sha256((ROOT / k).read_bytes()).hexdigest() for k in files}}
    target = ROOT / "manifest.json"
    target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    target.chmod(0o600)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    for component in ("mysql", "redis", "worker", "flink"):
        parser.add_argument("--" + component + "-image", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.run_id, {k: getattr(args, k + "_image") for k in ("mysql", "redis", "worker", "flink")})
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({
            "error": "dev_prepare_failed",
            "reason": safe_prepare_failure_detail(error),
        }), file=sys.stderr)
        raise SystemExit("Dev pipeline preparation failed; preserve evidence and inspect private files") from None
