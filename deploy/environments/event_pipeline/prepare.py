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


def compose(images):
    if set(images) != {"mysql", "redis", "worker"} or any(
        not re.fullmatch(r"sha256:[0-9a-f]{64}", v or "") for v in images.values()
    ):
        raise ValueError("three immutable image identities required")
    common = {"networks": ["internal"], "labels": {"binhu.environment": "development"},
              "restart": "on-failure:3", "pull_policy": "never", "pids_limit": 128,
              "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}},
              "security_opt": ["no-new-privileges:true"]}
    services = {
        "dev-derived-mysql": {**common, "image": images["mysql"], "env_file": ["mysql.env"],
            "mem_limit": "512m", "cpus": .5,
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
            "depends_on": ["dev-derived-mysql", "dev-derived-redis"]}
    return {"name": PROJECT, "services": services,
            "networks": {"internal": {"external": True, "name": NETWORK}},
            "volumes": {name: {"labels": {"binhu.environment": "development"}} for name in ("mysql", "redis")}}


def checked(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("Dev resource inspection failed")
    return result.stdout


def preflight():
    if ROOT.is_symlink() or ROOT.parent.is_symlink() or ROOT.resolve() != ROOT or ROOT.exists():
        raise ValueError("new absolute Dev output required")
    if checked(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]).strip():
        raise ValueError("existing project requires separate review")
    network = json.loads(checked(["docker", "network", "inspect", NETWORK]))[0]
    if not network.get("Internal") or network.get("Labels", {}).get("com.docker.compose.project") != "binhu-development-eventbus":
        raise ValueError("isolated Dev eventbus network required")
    for item in network.get("Containers", {}).values():
        if not item["Name"].startswith("binhu-development-"):
            raise ValueError("foreign network dependency")
    volumes = checked(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines()
    if any(v.startswith(PROJECT + "_") for v in volumes):
        raise ValueError("old volumes must not be reused")
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    if int(memory["MemAvailable"].split()[0]) < 3 * 1024**2:
        raise ValueError("insufficient memory reserve")
    for path in (ROOT.parent, Path("/data/docker")):
        stats = os.statvfs(path)
        if stats.f_bavail * stats.f_frsize < 10 * 1024**3:
            raise ValueError("insufficient disk reserve")


def prepare(run_id, images):
    spec = compose(images)
    db_password, redis_password, root_password = [secrets.token_hex(24) for _ in range(3)]
    env = {"APP_ENVIRONMENT": "development", "DEV_RUN_ID": run_id,
           "MYSQL_HOST": "dev-derived-mysql", "MYSQL_DATABASE": "Dev_EventPipeline",
           "MYSQL_USER": "dev_pipeline", "MYSQL_PASSWORD": db_password,
           "REDIS_HOST": "dev-derived-redis", "REDIS_PASSWORD": redis_password,
           "KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9092,kafka-2:9092,kafka-3:9092"}
    configuration(env)
    preflight()
    for image in images.values():
        if checked(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip() != image:
            raise ValueError("image identity mismatch")
    ROOT.mkdir(mode=0o700)
    sql = f"""USE Dev_EventPipeline;
CREATE TABLE _pipeline_identity (id INT PRIMARY KEY, environment VARCHAR(32), run_id VARCHAR(80), database_name VARCHAR(64));
INSERT INTO _pipeline_identity VALUES (1,'development','{run_id}','Dev_EventPipeline');
{SCHEMA_SQL};
CREATE TABLE dev_task_revisions (
 run_id VARCHAR(80) NOT NULL, task_id VARCHAR(96) NOT NULL,
 source_id BIGINT NOT NULL, revision BIGINT NOT NULL,
 PRIMARY KEY (run_id,task_id,source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
"""
    files = {
        "compose.json": json.dumps(spec, indent=2), "init.sql": sql,
        "runtime.env": "\n".join(f"{k}={v}" for k, v in env.items()) + "\n",
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
    for component in ("mysql", "redis", "worker"):
        parser.add_argument("--" + component + "-image", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.run_id, {k: getattr(args, k + "_image") for k in ("mysql", "redis", "worker")})
        print(json.dumps(result))
    except Exception:
        raise SystemExit("Dev pipeline preparation failed; preserve evidence and inspect private files") from None
