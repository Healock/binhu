"""Prepare a new Dev pipeline project from explicit images, without starting it."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

from .runtime import configuration
from .services.kafka_delivery_store import SCHEMA_SQL
from .flink_sql import render

PROJECT = "binhu-development-pipeline"
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
    }
)
# The relay was introduced after the first trusted Dev project was created.
# Its legacy container can therefore lack the environment label; Compose will
# recreate it from the current definition, which carries the label.
LEGACY_MISSING_ENV_LABEL_SERVICES = frozenset({"backend-outbox-relay"})


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
            "mem_limit": "512m", "cpus": .5,
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
        "env_file": ["runtime.env"], "mem_limit": "160m", "cpus": .25,
        "read_only": True, "tmpfs": ["/tmp:size=16m"],
        "command": ["python", "-m", "event_pipeline.runtime", "python-metadata-worker"],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "depends_on": {"dev-derived-mysql": {"condition": "service_healthy"}}}
    services["dual-track-monitor"] = {**common, "image": images["worker"],
        "env_file": ["runtime.env"], "mem_limit": "128m", "cpus": .2,
        "read_only": True, "tmpfs": ["/tmp:size=16m"],
        "volumes": ["evidence:/var/lib/binhu-dev-event-pipeline/evidence"],
        "command": ["python", "-m", "event_pipeline.runtime", "dual-track-monitor"],
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
    }
    for name, content in files.items():
        target = ROOT / name
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)
    # Parent directory stays 0700. Container service users need read permission
    # on the explicitly mounted files; they cannot enumerate the host parent.
    (ROOT / "init.sql").chmod(0o644)
    (ROOT / "redis.conf").chmod(0o644)
    (ROOT / "pipeline.sql").chmod(0o644)
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
    except Exception:
        raise SystemExit("Dev pipeline preparation failed; preserve evidence and inspect private files") from None
