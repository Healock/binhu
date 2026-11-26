"""Prepare and switch the Staging application to a verified task projection."""
from __future__ import annotations

import copy
import hmac
import json
from pathlib import Path
import re
import subprocess
import time

from .artifact import file_hash, write_json
from .event_pipeline import staging_backend_network, staging_control
from .runtime import root_for
from .update import (
    EVIDENCE_ROOT, backup_databases, check_resources, parse_environment, read_configuration,
    replace_file, wait_healthy,
)


RUN_RE = re.compile(r"STG-[0-9]{8}-[0-9]{2}")
PIPELINE_STATE = Path("/var/lib/binhu-staging-event-pipeline")
ACTIONS = frozenset({"backfill", "measure", "activate", "deactivate"})
REDIS_CAPACITY_MIB = 3072
REDIS_CONTAINER_LIMIT_MIB = 6144
BACKFILL_TIMEOUT_SECONDS = 3600
BACKFILL_LOCK_RETRY_DELAYS = (2, 5, 10)
_MYSQL_LOCK_ERROR = re.compile(r"(?:aiomysql|pymysql)\.err\.OperationalError: \((1205|1213), .+\)")


def _run(args: list[str], timeout: int = 120) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError("staging_task_read_model_command_failed")
    return result.stdout


def _verify_pipeline(run_id: str, application: dict, values: dict[str, str]) -> str:
    current = PIPELINE_STATE / "current.json"
    if PIPELINE_STATE.is_symlink() or current.is_symlink() or not current.is_file():
        raise ValueError("staging_task_pipeline_candidate_missing")
    pipeline = json.loads(current.read_text(encoding="utf-8"))
    if (pipeline.get("environment") != "staging" or pipeline.get("run_id") != run_id
            or pipeline.get("commit") != application["commit"]
            or pipeline.get("candidate_image_digest") != application["image_id"]
            or pipeline.get("started") is not True
            or pipeline.get("fictional_only") is not True
            or pipeline.get("production_data") is not False):
        raise ValueError("staging_task_pipeline_candidate_mismatch")
    pipeline_root, runtime, _ = staging_control._load(run_id)
    if (runtime.get("started") is not True or runtime.get("images") != pipeline.get("images")
            or runtime.get("staging_snapshot_id") != pipeline.get("staging_snapshot_id")):
        raise ValueError("staging_task_pipeline_runtime_mismatch")
    if "flink-task.env" not in runtime.get("hashes", {}):
        raise ValueError("staging_task_pipeline_sink_identity_missing")
    sink = parse_environment((pipeline_root / "flink-task.env").read_text(encoding="utf-8"))
    event_key = values.get("TASK_EVENT_ENCRYPTION_KEY", "")
    if (sink.get("APP_ENVIRONMENT") != "staging" or sink.get("STAGING_RUN_ID") != run_id
            or len(event_key.encode("utf-8")) < 32
            or not hmac.compare_digest(event_key.encode("utf-8"),
                                       sink.get("TASK_EVENT_ENCRYPTION_KEY", "").encode("utf-8"))):
        raise ValueError("staging_task_pipeline_sink_identity_mismatch")
    staging_control.measure(run_id)
    return runtime["images"]["worker"]


def _restore_business_bridge(backend: str, run_id: str, worker_image: str) -> bool:
    """Resume the current bridge without recreating its consumer group."""
    staging_backend_network.verify()
    project = staging_control.project_for(run_id)
    service = "business-bridge"
    name = f"{project}-{service}-1"
    expected_networks = {staging_control.network_for(run_id), staging_backend_network.NETWORK}

    def inspect_bridge(container: str) -> tuple[str, str]:
        # Select only identity fields. The container environment holds credentials.
        template = (
            '[{{json .Id}},{{json .Name}},{{json .Image}},{{json .Config.Labels}},'
            '{{json .Config.Cmd}},{{json .State.Status}},{{json .State.OOMKilled}},'
            '{{json .NetworkSettings.Networks}}]'
        )
        cid, actual_name, image, labels, command, status, oom, networks = json.loads(
            _run(["docker", "inspect", "--type", "container", "--format", template, container])
        )
        if (not re.fullmatch(r"[0-9a-f]{64}", cid) or actual_name != "/" + name
                or image != worker_image or not staging_control._staging_backend_connector(name, labels)
                or command != ["python", "-m", "event_pipeline.runtime", service]
                or set(networks or {}) != expected_networks or oom is not False):
            raise ValueError("staging_task_business_bridge_identity_mismatch")
        return cid, status

    cid, status = inspect_bridge(name)
    if status not in {"running", "exited"}:
        raise ValueError("staging_task_business_bridge_unavailable")
    # Starting with a missing group would create it at `$` and skip events
    # already published by the relay, including a partially completed backfill.
    probe = (
        "import sys; from config import settings; from redis import Redis; "
        "assert settings.APP_ENVIRONMENT == 'staging'; "
        "client = Redis.from_url(settings.REDIS_URL, decode_responses=True, "
        "socket_connect_timeout=3, socket_timeout=5); "
        "print(sum(group['name'] == sys.argv[1] for group in client.xinfo_groups('binhu:events')))"
    )
    if _run(["docker", "exec", backend, "python", "-c", probe, f"{run_id}-{service}"], timeout=10).strip() != "1":
        raise ValueError("staging_task_business_bridge_cursor_missing")
    if status == "running":
        return False
    _run(["docker", "start", cid], timeout=30)
    time.sleep(6)
    if inspect_bridge(cid) != (cid, "running"):
        raise ValueError("staging_task_business_bridge_restart_failed")
    return True


def _backend_id(root: Path, image_id: str) -> str:
    container = _run(["docker", "compose", "-f", str(root / "compose.json"),
                      "ps", "-q", "backend"]).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container):
        raise ValueError("staging_task_backend_missing")
    expected = "staging binhu-staging backend " + image_id + " running"
    inspected = _run(["docker", "inspect", "--format",
                      '{{index .Config.Labels "binhu.environment"}} '
                      '{{index .Config.Labels "com.docker.compose.project"}} '
                      '{{index .Config.Labels "com.docker.compose.service"}} '
                      '{{.Image}} {{.State.Status}}', container]).strip()
    if inspected != expected:
        raise ValueError("staging_task_backend_identity_mismatch")
    return container


def _read_model_command(container: str, run_id: str, module: str, action: str) -> dict:
    result = json.loads(_run(["docker", "exec", "-e", f"TASK_READ_MODEL_RUN_ID={run_id}",
                             container, "python", "-B", "-m", module, action], timeout=600))
    if result.get("environment") != "staging" or result.get("run_id") != run_id:
        raise ValueError("staging_task_read_model_result_mismatch")
    return result


def _backfill(container: str, run_id: str, evidence: Path) -> dict:
    command = ["docker", "exec", "-e", f"TASK_READ_MODEL_RUN_ID={run_id}", container,
               "python", "-B", "-m", "migrations.task_read_model_backfill", "apply"]
    deadline = time.monotonic() + BACKFILL_TIMEOUT_SECONDS
    retries = []
    for attempt in range(1 + len(BACKFILL_LOCK_RETRY_DELAYS)):
        report = evidence / f"backfill-attempt-{attempt + 1:02d}.json"
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            write_json(report, {"lock_errors": retries, "result": "timeout"})
            raise ValueError("staging_task_backfill_timeout") from None
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            if payload.get("environment") != "staging" or payload.get("run_id") != run_id:
                raise ValueError("staging_task_read_model_result_mismatch")
            write_json(report, {"lock_errors": retries, "result": "completed"})
            return payload
        lines = result.stderr.rstrip().splitlines()
        error = _MYSQL_LOCK_ERROR.fullmatch(lines[-1]) if lines else None
        if not error:
            write_json(report, {"lock_errors": retries, "result": "failed"})
            raise ValueError("staging_task_backfill_command_failed")
        retries.append(int(error[1]))
        if (attempt == len(BACKFILL_LOCK_RETRY_DELAYS)
                or time.monotonic() + BACKFILL_LOCK_RETRY_DELAYS[attempt] >= deadline):
            write_json(report, {"lock_errors": retries, "result": "lock_retry_exhausted"})
            raise ValueError("staging_task_backfill_lock_retry_exhausted")
        # The migration rolls back the failed batch. Its committed batches are
        # keyed by run, task, and revision, so the same command safely resumes.
        write_json(report, {"lock_errors": retries, "result": "retrying"})
        time.sleep(BACKFILL_LOCK_RETRY_DELAYS[attempt])


def _evidence(application: dict, run_id: str, action: str) -> Path:
    directory = EVIDENCE_ROOT / "staging-task-read-model"
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("staging_task_read_model_evidence_path_invalid")
    directory.mkdir(mode=0o700, exist_ok=True)
    evidence = directory / f"{run_id}-{application['run_id']}-{action}-{time.time_ns()}"
    evidence.mkdir(mode=0o700)
    return evidence


def _redis_capacity_compose(compose: dict) -> dict | None:
    redis = compose.get("services", {}).get("redis", {})
    command = redis.get("command")
    if (redis.get("labels", {}).get("binhu.environment") != "staging"
            or redis.get("volumes") != ["redis:/data"] or redis.get("networks") != ["internal"]
            or compose.get("volumes", {}).get("redis", {}).get("name") != "binhu-staging_redis"
            or not isinstance(command, list) or not command or command[0] != "redis-server"
            or command.count("--maxmemory") != 1
            or command.count("--maxmemory-policy") != 1
            or command.count("--appendonly") != 1
            or command[command.index("--appendonly") + 1:][:1] != ["yes"]
            or command[command.index("--maxmemory-policy") + 1:][:1] != ["noeviction"]):
        raise ValueError("staging_task_redis_capacity_contract_invalid")
    index = command.index("--maxmemory") + 1
    current = (command[index:index + 1], redis.get("mem_limit"))
    target = ([f"{REDIS_CAPACITY_MIB}mb"], f"{REDIS_CONTAINER_LIMIT_MIB}m")
    if current == target:
        return None
    if current not in ((["96mb"], "192m"), (["512mb"], "1024m")):
        raise ValueError("staging_task_redis_capacity_unreviewed")
    candidate = copy.deepcopy(compose)
    updated = candidate["services"]["redis"]
    updated["command"][index] = target[0][0]
    updated["mem_limit"] = target[1]
    return candidate


def _verify_redis_capacity(backend: str, expected_mib: int) -> None:
    probe = (
        "import json; from config import settings; from redis import Redis; "
        "assert settings.APP_ENVIRONMENT == 'staging'; "
        "client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=3, socket_timeout=5); "
        "memory = client.info('memory'); "
        "print(json.dumps([memory['maxmemory'], memory['maxmemory_policy']]))"
    )
    for attempt in range(12):
        try:
            state = json.loads(_run(["docker", "exec", backend, "python", "-c", probe], timeout=10))
            if state == [expected_mib * 1024 ** 2, "noeviction"]:
                return
        except (ValueError, subprocess.TimeoutExpired):
            pass
        if attempt < 11:
            time.sleep(2)
    raise ValueError("staging_task_redis_capacity_unverified")


def _prepare_redis_capacity(root: Path, manifest: dict, compose: dict,
                            evidence: Path, backend: str) -> dict:
    candidate = _redis_capacity_compose(compose)
    if candidate is None:
        _verify_redis_capacity(backend, REDIS_CAPACITY_MIB)
        return manifest
    check_resources(root)
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    if int(memory["MemAvailable"].split()[0]) < (REDIS_CONTAINER_LIMIT_MIB + 2048) * 1024:
        raise ValueError("staging_task_redis_host_reserve_insufficient")
    previous_command = compose["services"]["redis"]["command"]
    previous_mib = int(previous_command[previous_command.index("--maxmemory") + 1].removesuffix("mb"))
    staging_backend_network.verify()
    old_compose = evidence / "previous-compose.json"
    old_manifest = evidence / "previous-redis-manifest.json"
    old_compose.write_bytes((root / "compose.json").read_bytes())
    old_manifest.write_bytes((root / "manifest.json").read_bytes())
    new_compose = evidence / "redis-compose.json"
    new_manifest = evidence / "redis-manifest.json"
    write_json(new_compose, candidate)
    updated = copy.deepcopy(manifest)
    updated["hashes"]["compose.json"] = file_hash(new_compose)
    write_json(new_manifest, updated)
    docker = ["docker", "compose", "--project-directory", str(root)]
    _run([*docker, "-f", str(new_compose), "config", "--quiet"])

    def recreate_redis(capacity_mib: int) -> None:
        _run([*docker, "-f", str(root / "compose.json"), "up", "-d", "--no-deps", "redis"])
        _verify_redis_capacity(backend, capacity_mib)
        wait_healthy("staging", manifest["version"])
        staging_backend_network.apply()

    try:
        replace_file(new_compose, root / "compose.json")
        replace_file(new_manifest, root / "manifest.json")
        recreate_redis(REDIS_CAPACITY_MIB)
    except Exception:
        try:
            replace_file(old_compose, root / "compose.json")
            replace_file(old_manifest, root / "manifest.json")
            recreate_redis(previous_mib)
        except Exception:
            write_json(evidence / "redis-capacity.json", {"rollback": "failed"})
            raise ValueError("staging_task_redis_capacity_restore_failed") from None
        write_json(evidence / "redis-capacity.json", {"rollback": "healthy"})
        raise ValueError("staging_task_redis_capacity_failed") from None
    write_json(evidence / "redis-capacity.json", {
        "redis_capacity_mib": REDIS_CAPACITY_MIB,
        "container_limit_mib": REDIS_CONTAINER_LIMIT_MIB,
    })
    return updated


def _configure(root: Path, manifest: dict, env_text: str, run_id: str,
               enabled: bool, evidence: Path) -> None:
    values = parse_environment(env_text)
    values.update({"TASK_READ_MODEL_RUN_ID": run_id, "TASK_READ_MODEL_ENABLED": str(enabled).lower()})
    previous_env = evidence / "previous.env"
    previous_env.write_text(env_text, encoding="utf-8")
    previous_manifest = evidence / "previous.json"
    write_json(previous_manifest, manifest)
    candidate_env = evidence / "candidate.env"
    candidate_env.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    candidate_manifest = evidence / "candidate.json"
    write_json(candidate_manifest, {
        **manifest, "hashes": {**manifest["hashes"], "backend.env": file_hash(candidate_env)},
    })
    compose = ["docker", "compose", "-f", str(root / "compose.json"),
               "up", "-d", "--no-deps", "--force-recreate", "backend"]
    try:
        replace_file(candidate_env, root / "backend.env")
        replace_file(candidate_manifest, root / "manifest.json")
        _run(compose)
        wait_healthy("staging", manifest["version"])
        _backend_id(root, manifest["images"]["backend"])
    except Exception:
        try:
            replace_file(previous_env, root / "backend.env")
            replace_file(previous_manifest, root / "manifest.json")
            _run(compose)
            wait_healthy("staging", manifest["version"])
        except Exception:
            write_json(evidence / "failure.json", {"rollback": "failed"})
            raise ValueError("staging_task_read_model_restore_failed") from None
        write_json(evidence / "failure.json", {"rollback": "healthy"})
        raise ValueError("staging_task_read_model_switch_failed") from None


def run(application: dict, run_id: str, action: str) -> dict:
    if not RUN_RE.fullmatch(run_id) or action not in ACTIONS:
        raise ValueError("staging_task_read_model_input_invalid")
    import fcntl

    root = root_for("staging")
    with (root / ".deployment.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest, compose, env_text = read_configuration(root)
        values = parse_environment(env_text)
        if (manifest.get("environment") != "staging" or values.get("APP_ENVIRONMENT") != "staging"
                or compose.get("name") != "binhu-staging"
                or manifest.get("artifact_id") != application["artifact_id"]
                or manifest.get("commit") != application["commit"]
                or manifest.get("images", {}).get("backend") != application["image_id"]):
            raise ValueError("staging_task_application_candidate_mismatch")
        selected = values.get("TASK_READ_MODEL_RUN_ID", "")
        enabled = values.get("TASK_READ_MODEL_ENABLED", "false").lower() == "true"
        container = _backend_id(root, application["image_id"])
        if action == "deactivate":
            if selected != run_id:
                raise ValueError("staging_task_read_model_run_mismatch")
            if enabled:
                evidence = _evidence(application, run_id, action)
                _configure(root, manifest, env_text, run_id, False, evidence)
            return {"environment": "staging", "run_id": run_id, "read_model_enabled": False}

        worker_image = _verify_pipeline(run_id, application, values)
        if action == "backfill":
            if enabled:
                raise ValueError("staging_task_read_model_already_enabled")
            evidence = _evidence(application, run_id, action)
            backup_databases("staging", root, manifest, evidence)
            manifest = _prepare_redis_capacity(root, manifest, compose, evidence, container)
            if selected != run_id:
                _configure(root, manifest, env_text, run_id, False, evidence)
                container = _backend_id(root, application["image_id"])
            restored = _restore_business_bridge(container, run_id, worker_image)
            write_json(evidence / "business-bridge.json", {"cursor_verified": True, "restarted": restored})
            report = _backfill(container, run_id, evidence)
            return {"environment": "staging", "run_id": run_id, "read_model_enabled": False,
                    **{key: int(report[key]) for key in ("total", "emitted", "already_emitted")}}
        if selected != run_id:
            raise ValueError("staging_task_read_model_run_mismatch")
        report = _read_model_command(container, run_id, "migrations.task_read_model_activation",
                                     "verify" if action == "activate" else "measure")
        if action == "activate":
            if (report.get("ready") is not True or report.get("readiness_marked") is not True
                    or report.get("flink_checkpoint_live") is not True):
                raise ValueError("staging_task_read_model_not_ready")
            if not enabled:
                evidence = _evidence(application, run_id, action)
                _configure(root, manifest, env_text, run_id, True, evidence)
                enabled = True
        return {"environment": "staging", "run_id": run_id, "read_model_enabled": enabled,
                "ready": report["ready"] is True, "flink_checkpoint_live": report["flink_checkpoint_live"] is True,
                "mismatch_count": int(report["mismatch_count"])}
