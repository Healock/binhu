"""Prepare one isolated Staging event-pipeline runtime without starting it.

Credentials are supplied only by the server-side Staging gateway.  Candidate
archives contain source, the compiled Flink job, immutable image identities,
and no secret values.
"""
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
from typing import Iterable

from .flink_sql import render
from .runtime import backend_relay_configuration, configuration
from .services.kafka_delivery_store import SCHEMA_SQL
from .staging_compose import IMAGE_KEYS, compose, model_sha256, project_for


BASE = Path("/srv/binhu-environments/staging-event-pipeline")
SECRET_RE = re.compile(r"^[0-9a-f]{48}$")
PUBLIC_FILES = frozenset({"compose.json", "init.sql", "redis.conf", "pipeline.sql", "pipeline-job.jar"})


def root_for(run_id: str) -> Path:
    return BASE / run_id


def checked(command: list[str], *, timeout: int = 60) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError("Staging preparation command failed")
    return result.stdout


def redis_configuration(password: str) -> str:
    if not SECRET_RE.fullmatch(password or ""):
        raise ValueError("independent Staging Redis credential required")
    return (
        "bind 0.0.0.0\nprotected-mode yes\n"
        f"requirepass {password}\n"
        "maxmemory 192mb\nmaxmemory-policy noeviction\n"
        "appendonly yes\nappendfsync everysec\n"
        "auto-aof-rewrite-percentage 100\nauto-aof-rewrite-min-size 16mb\n"
    )


def init_sql(run_id: str) -> str:
    # Table names stay stable because both engines write separate, explicitly
    # named projections.  Environment and run_id provide the hard fence.
    return f"""USE Staging_EventPipeline;
CREATE TABLE _pipeline_identity (id INT PRIMARY KEY, environment VARCHAR(32), run_id VARCHAR(80), database_name VARCHAR(64));
INSERT INTO _pipeline_identity VALUES (1,'staging','{run_id}','Staging_EventPipeline');
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


def runtime_environment(run_id: str, *, mysql_password: str, redis_password: str,
                        backend_redis_url: str) -> dict[str, str]:
    env = {
        "APP_ENVIRONMENT": "staging", "PIPELINE_RUN_ID": run_id, "STAGING_RUN_ID": run_id,
        "MYSQL_HOST": "staging-derived-mysql", "MYSQL_DATABASE": "Staging_EventPipeline",
        "MYSQL_USER": "staging_pipeline", "MYSQL_PASSWORD": mysql_password,
        "REDIS_HOST": "staging-derived-redis", "REDIS_PASSWORD": redis_password,
        "KAFKA_BOOTSTRAP_SERVERS": "staging-kafka-1:9092,staging-kafka-2:9092,staging-kafka-3:9092",
        "BACKEND_REDIS_URL": backend_redis_url, "BACKEND_REDIS_STREAM_KEY": "binhu:events",
        "BACKEND_REDIS_START_ID": "$",
    }
    configuration(env)
    return env


def snapshot_database(snapshot_id: str, domain: str) -> str:
    if not re.fullmatch(r"staging-[0-9a-f]{16}", snapshot_id):
        raise ValueError("Staging snapshot identity invalid")
    if domain not in {"OnlineData"}:
        raise ValueError("unsupported Staging snapshot domain")
    return f"Staging_s{snapshot_id[8:]}_{domain}"


def backend_environment(run_id: str, *, password: str, redis_url: str,
                        snapshot_id: str) -> dict[str, str]:
    env = {
        "APP_ENVIRONMENT": "staging", "PIPELINE_RUN_ID": run_id, "STAGING_RUN_ID": run_id,
        "BACKEND_MYSQL_HOST": "environment-mysql",
        "BACKEND_MYSQL_DATABASE": snapshot_database(snapshot_id, "OnlineData"),
        "BACKEND_MYSQL_USER": "environment_app", "BACKEND_MYSQL_PASSWORD": password,
        "BACKEND_REDIS_URL": redis_url, "BACKEND_REDIS_STREAM_KEY": "binhu:events",
    }
    backend_relay_configuration(env)
    return env


def _env_text(values: dict[str, str]) -> str:
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Staging credential contains a line break")
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def _copy_code(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("candidate event-pipeline source missing")
    destination.mkdir(mode=0o755)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in {"__pycache__", "target"} for part in relative.parts):
            continue
        target = destination / relative
        if path.is_symlink():
            raise ValueError("candidate source symlink refused")
        if path.is_dir():
            target.mkdir(mode=0o755, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o644)


def _hashes(root: Path, names: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        path = root / name
        if path.is_dir():
            for item in sorted(path.rglob("*")):
                if item.is_file() and not item.is_symlink():
                    result[item.relative_to(root).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
        else:
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def prepare(run_id: str, images: dict[str, str], *, source: Path, pipeline_jar: Path,
            snapshot_id: str,
            environ: dict[str, str] | None = None, verify_images: bool = True) -> dict:
    values = os.environ if environ is None else environ
    spec = compose(images, run_id)
    root = root_for(run_id)
    if root.exists() or root.is_symlink():
        raise ValueError("Staging run directory already exists")
    if BASE.is_symlink() or BASE.parent.is_symlink():
        raise ValueError("fixed Staging root required")
    if pipeline_jar.is_symlink() or not pipeline_jar.is_file():
        raise ValueError("compiled Staging PipelineJob JAR missing")
    if verify_images:
        for image in images.values():
            if checked(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip() != image:
                raise ValueError("Staging image identity mismatch")

    mysql_password = values.get("STAGING_PIPELINE_MYSQL_PASSWORD", "")
    mysql_root_password = values.get("STAGING_PIPELINE_MYSQL_ROOT_PASSWORD", "")
    redis_password = values.get("STAGING_PIPELINE_REDIS_PASSWORD", "")
    backend_password = values.get("STAGING_BACKEND_MYSQL_PASSWORD", "")
    backend_redis_url = values.get("STAGING_BACKEND_REDIS_URL", "")
    if not all(SECRET_RE.fullmatch(item or "") for item in (mysql_password, mysql_root_password, redis_password)):
        raise ValueError("independent Staging pipeline credentials required")
    runtime_env = runtime_environment(run_id, mysql_password=mysql_password,
                                      redis_password=redis_password,
                                      backend_redis_url=backend_redis_url)
    backend_env = backend_environment(
        run_id, password=backend_password, redis_url=backend_redis_url,
        snapshot_id=snapshot_id,
    )

    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.mkdir(mode=0o700)
    try:
        files = {
            "compose.json": json.dumps(spec, indent=2),
            "init.sql": init_sql(run_id),
            "runtime.env": _env_text(runtime_env),
            "backend-relay.env": _env_text(backend_env),
            "mysql.env": _env_text({
                "MYSQL_ROOT_PASSWORD": mysql_root_password,
                "MYSQL_DATABASE": "Staging_EventPipeline", "MYSQL_USER": "staging_pipeline",
                "MYSQL_PASSWORD": mysql_password,
            }),
            "redis.conf": redis_configuration(redis_password),
            "pipeline.sql": render(runtime_env),
        }
        for name, content in files.items():
            target = root / name
            target.write_text(content, encoding="utf-8")
            target.chmod(0o644 if name in PUBLIC_FILES else 0o600)
        jar_target = root / "pipeline-job.jar"
        shutil.copyfile(pipeline_jar, jar_target)
        jar_target.chmod(0o644)
        _copy_code(source, root / "code")
        hashes = _hashes(root, [*files, "pipeline-job.jar", "code"])
        manifest = {
            "schema": 1, "environment": "staging", "run_id": run_id,
            "project": project_for(run_id), "images": images,
            "staging_snapshot_id": snapshot_id,
            "compose_model_sha256": model_sha256(images, run_id),
            "started": False, "acceptance": "pending", "hashes": hashes,
        }
        target = root / "manifest.json"
        target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        target.chmod(0o600)
        return manifest
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--pipeline-jar", type=Path, required=True)
    parser.add_argument("--staging-snapshot-id", required=True)
    for name in sorted(IMAGE_KEYS):
        parser.add_argument(f"--{name.replace('_', '-')}-image", required=True)
    args = parser.parse_args()
    images = {name: getattr(args, name + "_image") for name in IMAGE_KEYS}
    try:
        print(json.dumps(prepare(args.run_id, images, source=args.source,
                                 pipeline_jar=args.pipeline_jar,
                                 snapshot_id=args.staging_snapshot_id), sort_keys=True))
    except Exception:
        raise SystemExit("Staging event-pipeline preparation refused; inspect private evidence") from None


if __name__ == "__main__":
    main()
