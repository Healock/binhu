"""Collect fixed, aggregate-only Staging load-test resource metrics."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
import urllib.error
import urllib.request

from .staging_compose import BROKERS, EVENT_TOPIC
from .staging_prepare import root_for


DERIVED_DATABASE = "Staging_EventPipeline"
PRODUCTION_CONTAINER_NAMES = ("binhu-backend", "binhu-mysql", "binhu-ops-agent")
PRODUCTION_BOOTSTRAP_URL = "http://127.0.0.1:37125/api/app/bootstrap"
PRODUCTION_HEALTH_URL = "http://127.0.0.1:37125/api/health"
PRODUCTION_ERROR_RE = re.compile(r"\b(?:ERROR|CRITICAL|FATAL|Traceback)\b", re.IGNORECASE)
PRODUCTION_LOG_LINE_LIMIT = 5000
PRODUCTION_LOG_BYTE_LIMIT = 8 * 1024 * 1024
PRODUCTION_MODE_RUNNING = "running"
PRODUCTION_MODE_PAUSED = "production_paused"
PRODUCTION_PAUSE_RUN_RE = re.compile(r"^PROD-PAUSE-[0-9]{8}-[0-9]{2}$")
AUTHORIZED_PRODUCTION_PAUSE_RUN_ID = "PROD-PAUSE-20261029-02"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PRODUCTION_PAUSE_EVIDENCE_ROOT = Path("/srv/binhu-release-evidence-maintenance")
PRODUCTION_PAUSE_BACKUP_ROOT = Path("/root/binhu/deploy-backups/automated")
PRODUCTION_PAUSE_DATABASES = (
    "DispatchData", "OnlineData", "OnlineDataArchive", "PlatformData",
    "RegistryData", "VisitData", "WorkflowData", "daily_report",
)


def _run(command: list[str], *, timeout: int = 30, input_text: str | None = None) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, input=input_text,
    )
    if result.returncode:
        raise RuntimeError("fixed Staging metrics command failed")
    return result.stdout


def _container(project: str, service: str) -> str:
    return f"{project}-{service}-1"


def _mysql_status(container: str) -> dict[str, int | None]:
    sql = (
        "SHOW GLOBAL STATUS WHERE Variable_name IN "
        "('Threads_connected','Threads_running','Innodb_row_lock_current_waits',"
        "'Innodb_row_lock_time_max','Innodb_deadlocks')"
    )
    output = _run([
        "docker", "exec", container, "sh", "-c",
        'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u"$MYSQL_USER" --batch --skip-column-names -e "$1"',
        "staging-metrics", sql,
    ])
    result: dict[str, int | None] = {}
    for line in output.splitlines():
        name, _, value = line.partition("\t")
        if name and value.strip().lstrip("-").isdigit():
            result[name] = int(value)
    required = {
        "Threads_connected", "Threads_running", "Innodb_row_lock_current_waits",
        "Innodb_row_lock_time_max",
    }
    if not required <= set(result):
        raise RuntimeError("Staging MySQL metrics contract incomplete")
    # MariaDB and some MySQL-compatible images do not expose the optional
    # cumulative deadlock counter. Preserve that as unavailable, not zero.
    result.setdefault("Innodb_deadlocks", None)
    return result


def _backend_pool(container: str, run_id: str) -> dict[str, int | float]:
    password = os.environ.get("STAGING_LOAD_TEST_PASSWORD", "")
    if len(password) < 16:
        raise RuntimeError("private Staging load-test password missing")
    script = r'''import http.cookies,json,sys,urllib.request
password=sys.stdin.readline().rstrip("\r\n")
login=urllib.request.Request(
    "http://127.0.0.1:37125/api/auth/login",
    data=json.dumps({"username":"staging-load-admin-01@staging","password":password,
                     "device_type":"staging-metrics","device_id":sys.argv[1].lower()+"-metrics"}).encode(),
    headers={"Content-Type":"application/json"}, method="POST")
with urllib.request.urlopen(login,timeout=15) as response:
    cookies=http.cookies.SimpleCookie()
    for value in response.headers.get_all("Set-Cookie",[]): cookies.load(value)
cookie="; ".join(name+"="+morsel.value for name,morsel in cookies.items())
request=urllib.request.Request("http://127.0.0.1:37125/api/admin/ops/performance?minutes=15",
                               headers={"Cookie":cookie})
with urllib.request.urlopen(request,timeout=20) as response: payload=json.load(response)
pools=payload.get("database",{}).get("pools",[])
if not pools: raise SystemExit("pool metrics missing")
ratios=[float(item.get("usage_percent") or 0)/100 for item in pools]
print(json.dumps({"pool_count":len(pools),"usage_ratio":max(ratios),
                  "used":sum(int(item.get("used") or 0) for item in pools),
                  "max_size":sum(int(item.get("max_size") or 0) for item in pools)}))'''
    output = _run(
        ["docker", "exec", "-i", container, "python", "-c", script, run_id],
        timeout=30, input_text=password + "\n",
    ).strip()
    password = ""
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        raise RuntimeError("Staging Backend pool metric is invalid") from None
    required = {"pool_count", "usage_ratio", "used", "max_size"}
    if not isinstance(result, dict) or set(result) != required or int(result["pool_count"]) <= 0:
        raise RuntimeError("Staging Backend pool metric is incomplete")
    return result


def _queue(container: str, run_id: str, database: str) -> dict[str, int]:
    if database != DERIVED_DATABASE:
        raise RuntimeError("Staging derived database identity invalid")
    sql = (
        f"SELECT COUNT(*) FROM `{database}`._kafka_event_delivery "
        f"WHERE run_id='{run_id}' AND status NOT IN ('published','discarded')"
    )
    output = _run([
        "docker", "exec", container, "sh", "-c",
        'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u"$MYSQL_USER" --batch --skip-column-names -e "$1"',
        "staging-queue", sql,
    ]).strip()
    if not output.isdigit():
        raise RuntimeError("Staging derived queue metric is invalid")
    return {"pending": int(output), "drain_seconds": 0}


def _redis(container: str) -> dict[str, int]:
    password = (
        os.environ.get("STAGING_PIPELINE_REDIS_PASSWORD")
        or os.environ.get("REDIS_PASSWORD", "")
    )
    if len(password) < 16:
        raise RuntimeError("private Staging Redis password missing")
    output = _run([
        "docker", "exec", "-i", container, "sh", "-c",
        'read -r REDISCLI_AUTH; export REDISCLI_AUTH; '
        'exec redis-cli --no-auth-warning INFO all',
    ], input_text=password + "\n")
    password = ""
    values: dict[str, int] = {}
    aliases = {
        "used_memory": "used_memory", "maxmemory": "maxmemory",
        "evicted_keys": "evicted_keys", "keyspace_hits": "keyspace_hits",
        "keyspace_misses": "keyspace_misses",
    }
    for line in output.splitlines():
        name, separator, value = line.strip().partition(":")
        if separator and name in aliases and value.strip().isdigit():
            values[aliases[name]] = int(value)
        elif separator and name == "errorstat_OOM":
            match = re.search(r"(?:^|,)count=(\d+)(?:,|$)", value)
            if match:
                values["oom_error_count"] = int(match.group(1))
    values.setdefault("oom_error_count", 0)
    required = {"used_memory", "maxmemory", "evicted_keys", "keyspace_hits", "keyspace_misses", "oom_error_count"}
    if set(values) != required:
        raise RuntimeError("Staging Redis metrics contract incomplete")
    return values


def _kafka(project: str, run_id: str) -> dict[str, int]:
    broker = _container(project, BROKERS[0])
    lag = 0
    groups = (f"{run_id}-flink", f"{run_id}-python-metadata")
    for group in groups:
        output = _run([
            "docker", "exec", broker, "/opt/kafka/bin/kafka-consumer-groups.sh",
            "--bootstrap-server", f"{BROKERS[0]}:9092", "--describe", "--group", group,
        ])
        group_rows = 0
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[1] == EVENT_TOPIC and parts[5].lstrip("-").isdigit():
                lag += max(0, int(parts[5]))
                group_rows += 1
        # A freshly started run has no committed offsets until its first
        # event is published. Treat that valid bootstrap state as zero lag;
        # subsequent samples report partition rows after consumers commit.
    return {"lag": lag, "group_count": len(groups)}


def _flink(project: str, run_id: str) -> dict[str, Any]:
    jobmanager = _container(project, "jobmanager")
    overview = json.loads(_run([
        "docker", "exec", jobmanager, "curl", "-fsS", "--max-time", "15",
        "http://127.0.0.1:8081/jobs/overview",
    ]))
    jobs = [item for item in overview.get("jobs", [])
            if run_id in str(item.get("name", "")) and item.get("state") == "RUNNING"]
    if len(jobs) != 1:
        raise RuntimeError("Staging Flink running job identity mismatch")
    jid = str(jobs[0].get("jid") or "")
    checkpoints = json.loads(_run([
        "docker", "exec", jobmanager, "curl", "-fsS", "--max-time", "15",
        f"http://127.0.0.1:8081/jobs/{jid}/checkpoints",
    ]))
    counts = checkpoints.get("counts", {}) or {}
    latest = checkpoints.get("latest", {}).get("completed") or {}
    details = json.loads(_run([
        "docker", "exec", jobmanager, "curl", "-fsS", "--max-time", "15",
        f"http://127.0.0.1:8081/jobs/{jid}",
    ]))
    ratios: list[float] = []
    for vertex in details.get("vertices", []) or []:
        vertex_id = str(vertex.get("id") or "")
        if not vertex_id:
            continue
        pressure = json.loads(_run([
            "docker", "exec", jobmanager, "curl", "-fsS", "--max-time", "15",
            f"http://127.0.0.1:8081/jobs/{jid}/vertices/{vertex_id}/backpressure",
        ]))
        for subtask in pressure.get("subtasks", []) or []:
            for key in ("backpressureRatio", "ratio"):
                value = subtask.get(key)
                if isinstance(value, (int, float)):
                    ratios.append(float(value))
        if not pressure.get("subtasks"):
            level = str(pressure.get("backpressure-level") or pressure.get("backpressureLevel") or "ok").lower()
            ratios.append({"high": 1.0, "low": .5, "ok": 0.0}.get(level, 0.0))
    return {
        "checkpoint_completed": int(counts.get("completed") or 0),
        "checkpoint_failed": int(counts.get("failed") or 0),
        "checkpoint_duration_ms": int(latest.get("duration") or 0),
        "backpressure_ratio": max(ratios, default=0.0),
    }


def _containers(project: str) -> dict[str, Any]:
    ids = _run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]).split()
    if not ids:
        raise RuntimeError("Staging pipeline containers missing")
    inspected = json.loads(_run(["docker", "inspect", *ids]))
    return {
        "count": len(inspected),
        "restart_count": sum(int(item.get("RestartCount") or 0) for item in inspected),
        "oom_killed_count": sum(1 for item in inspected if item.get("State", {}).get("OOMKilled")),
        "running_count": sum(1 for item in inspected if item.get("State", {}).get("Running")),
    }


def _production_http(url: str) -> tuple[int, dict[str, Any] | None]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            status = int(response.status)
            if url == PRODUCTION_BOOTSTRAP_URL and status == 200:
                payload = json.loads(response.read(64 * 1024))
                return status, payload if isinstance(payload, dict) else None
            return status, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return 0, None


def _production_log_error_count(name: str, since_unix: int) -> int:
    command = ["docker", "logs", "--since", str(since_unix), "--tail",
               str(PRODUCTION_LOG_LINE_LIMIT + 1), name]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if process.stdout is None:
        raise RuntimeError("production_log_monitor_unavailable")
    output = process.stdout.read(PRODUCTION_LOG_BYTE_LIMIT + 1)
    if len(output) > PRODUCTION_LOG_BYTE_LIMIT:
        process.kill()
        process.wait(timeout=3)
        raise RuntimeError("production_log_sample_limit_exceeded")
    try:
        return_code = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
        raise RuntimeError("production_log_monitor_timeout") from None
    if return_code:
        raise RuntimeError("production_log_monitor_unavailable")
    lines = output.decode("utf-8", "replace").splitlines()
    if len(lines) > PRODUCTION_LOG_LINE_LIMIT:
        raise RuntimeError("production_log_sample_limit_exceeded")
    return sum(1 for line in lines if PRODUCTION_ERROR_RE.search(line))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_pause_evidence(pause_run_id: str) -> dict[str, Any]:
    """Validate the fixed, aggregate-only Production pause contract."""
    if (not PRODUCTION_PAUSE_RUN_RE.fullmatch(pause_run_id)
            or pause_run_id != AUTHORIZED_PRODUCTION_PAUSE_RUN_ID):
        raise RuntimeError("production_pause_run_id_invalid")
    evidence_path = PRODUCTION_PAUSE_EVIDENCE_ROOT / "pause-production" / pause_run_id / "pause.json"
    backup_dir = PRODUCTION_PAUSE_BACKUP_ROOT / f"maintenance-pause-production-{pause_run_id}"
    manifest_path = backup_dir / "backup-manifest.json"
    for path in (evidence_path, manifest_path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise RuntimeError("production_pause_evidence_missing")
    try:
        pause = json.loads(evidence_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("production_pause_evidence_invalid") from error
    if (not isinstance(pause, dict) or pause.get("schema") != 1
            or pause.get("operation") != "pause-production"
            or pause.get("run_id") != pause_run_id
            or pause.get("accepted") is not True
            or pause.get("protected_container_states_unchanged") is not True
            or sorted(pause.get("stopped_services") or []) != ["backend", "mysql"]):
        raise RuntimeError("production_pause_evidence_invalid")
    expected_scope = list(PRODUCTION_PAUSE_DATABASES)
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1
            or manifest.get("operation") != "pause-production"
            or manifest.get("run_id") != pause_run_id
            or manifest.get("database_scope") != expected_scope):
        raise RuntimeError("production_pause_backup_scope_mismatch")
    entries = manifest.get("databases")
    if (not isinstance(entries, list) or len(entries) != len(expected_scope)
            or {item.get("database") for item in entries if isinstance(item, dict)}
            != set(expected_scope)):
        raise RuntimeError("production_pause_backup_scope_mismatch")
    for item in entries:
        if not isinstance(item, dict):
            raise RuntimeError("production_pause_backup_manifest_invalid")
        database = item.get("database")
        backup_path = backup_dir / f"{database}.sql.gz"
        try:
            valid = (
                backup_path.is_file() and not backup_path.is_symlink()
                and item.get("filename") == backup_path.name
                and SHA256_RE.fullmatch(str(item.get("sha256") or "")) is not None
                and int(item.get("size_bytes") or -1) == backup_path.stat().st_size
                and _sha256_file(backup_path) == item["sha256"]
            )
        except (OSError, TypeError, ValueError, KeyError):
            valid = False
        if not valid:
            raise RuntimeError("production_pause_backup_manifest_invalid")
    manifest_sha256 = _sha256_file(manifest_path)
    if pause.get("backup_manifest_sha256") != manifest_sha256:
        raise RuntimeError("production_pause_evidence_mismatch")
    return {
        "pause_run_id": pause_run_id,
        "pause_evidence_valid": True,
        "pause_evidence_sha256": _sha256_file(evidence_path),
        "backup_manifest_sha256": manifest_sha256,
        "backup_database_count": len(expected_scope),
        "backup_database_scope": expected_scope,
    }


def _production_sample(since_unix: int, production_mode: str = PRODUCTION_MODE_RUNNING,
                       pause_run_id: str | None = None) -> dict[str, Any]:
    """Return fixed aggregate-only Production health and container evidence."""
    try:
        if production_mode not in {PRODUCTION_MODE_RUNNING, PRODUCTION_MODE_PAUSED}:
            raise RuntimeError("production_monitor_mode_invalid")
        if (not isinstance(since_unix, int) or since_unix <= 0
                or since_unix > int(time.time()) or since_unix < int(time.time()) - 3600):
            raise RuntimeError("production_monitor_start_time_invalid")
        inspected = json.loads(_run(["docker", "inspect", *PRODUCTION_CONTAINER_NAMES]))
        if len(inspected) != len(PRODUCTION_CONTAINER_NAMES):
            raise RuntimeError("production_container_set_incomplete")
        states: dict[str, dict[str, Any]] = {}
        for name, item in zip(PRODUCTION_CONTAINER_NAMES, inspected, strict=True):
            if item.get("Name", "").lstrip("/") != name:
                raise RuntimeError("production_container_identity_mismatch")
            state = item.get("State") or {}
            health = state.get("Health") or {}
            states[name] = {
                "running": bool(state.get("Running")),
                "restarting": bool(state.get("Restarting")),
                "oom_killed": bool(state.get("OOMKilled")),
                "health": str(health.get("Status") or ""),
                "restart_count": int(item.get("RestartCount") or 0),
            }
            if name == "binhu-backend":
                environment = {
                    value.partition("=")[2]
                    for value in (item.get("Config", {}).get("Env") or [])
                    if value.startswith("APP_ENVIRONMENT=")
                }
                if environment != {"production"}:
                    raise RuntimeError("production_backend_identity_mismatch")

        pause = {}
        if production_mode == PRODUCTION_MODE_PAUSED:
            if not isinstance(pause_run_id, str):
                raise RuntimeError("production_pause_run_id_invalid")
            pause = _read_pause_evidence(pause_run_id)
            bootstrap_status, health_status, bootstrap_environment = 0, 0, "production"
            if (not states["binhu-ops-agent"]["running"]
                    or states["binhu-backend"]["running"]
                    or states["binhu-mysql"]["running"]):
                raise RuntimeError("production_paused_state_changed")
        else:
            if pause_run_id is not None:
                raise RuntimeError("production_pause_run_id_unexpected")
            bootstrap_status, bootstrap = _production_http(PRODUCTION_BOOTSTRAP_URL)
            health_status, _ = _production_http(PRODUCTION_HEALTH_URL)
            bootstrap_environment = str((bootstrap or {}).get("environment") or "")
            if bootstrap_status != 200 or bootstrap_environment != "production":
                raise RuntimeError("production_bootstrap_identity_mismatch")
        error_count = 0
        for name in PRODUCTION_CONTAINER_NAMES:
            error_count += _production_log_error_count(name, since_unix)
        return {
            "environment": "production",
            "bootstrap_environment": bootstrap_environment,
            "bootstrap_status": bootstrap_status,
            "health_status": health_status,
            "health": 200 <= health_status < 300,
            "production_mode": production_mode,
            "containers": states,
            "error_count": error_count,
            "monitor_error": None,
            **pause,
        }
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        code = str(error) if str(error).startswith("production_") else "production_monitor_failed"
        return {
            "environment": "production", "bootstrap_environment": "",
            "bootstrap_status": 0, "health_status": 0, "health": False,
            "production_mode": production_mode,
            "pause_run_id": pause_run_id if production_mode == PRODUCTION_MODE_PAUSED else None,
            "containers": {}, "error_count": None, "monitor_error": code,
        }


def _staging_service_states() -> dict[str, Any]:
    names = ("binhu-staging-backend-1", "binhu-staging-environment-mysql-1")
    inspected = json.loads(_run(["docker", "inspect", *names]))
    if len(inspected) != len(names):
        raise RuntimeError("Staging application service set incomplete")
    result: dict[str, Any] = {}
    for name, item in zip(names, inspected, strict=True):
        if item.get("Name", "").lstrip("/") != name:
            raise RuntimeError("Staging application service identity mismatch")
        state = item.get("State") or {}
        result[name] = {
            "running": bool(state.get("Running")),
            "restarting": bool(state.get("Restarting")),
            "oom_killed": bool(state.get("OOMKilled")),
            "restart_count": int(item.get("RestartCount") or 0),
        }
    return result


def sample(run_id: str, production_since_unix: int,
           production_mode: str = PRODUCTION_MODE_RUNNING,
           production_pause_run_id: str | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"STG-[0-9]{8}-[0-9]{2}", run_id):
        raise RuntimeError("Staging metrics run id invalid")
    root = root_for(run_id)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("environment") != "staging" or manifest.get("run_id") != run_id:
        raise RuntimeError("Staging metrics identity mismatch")
    project = str(manifest["project"])
    app_mysql = "binhu-staging-environment-mysql-1"
    backend = "binhu-staging-environment-mysql-1"
    backend_env = Path("/srv/binhu-environments/staging/backend.env")
    if backend_env.is_symlink() or not backend_env.is_file():
        raise RuntimeError("Staging backend environment missing")
    derived_mysql = _container(project, "staging-derived-mysql")
    pipeline_redis = _container(project, "staging-derived-redis")
    stat = Path(root).stat()
    disk = Path(root).anchor or "/"
    usage = __import__("shutil").disk_usage(disk)
    meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {
        "sampled_at_unix": int(time.time()),
        "run_id": run_id,
        "environment": "staging",
        "production_data": False,
        "mysql": _mysql_status(app_mysql),
        "backend_pool": _backend_pool("binhu-staging-backend-1", run_id),
        "redis": _redis(pipeline_redis),
        "kafka": _kafka(project, run_id),
        "flink": _flink(project, run_id),
        "derived_queue": _queue(derived_mysql, run_id, DERIVED_DATABASE),
        "containers": _containers(project),
        "staging_services": _staging_service_states(),
        "host": {"memory_available_bytes": int(meminfo["MemAvailable"].split()[0]) * 1024},
        "disk": {"used": usage.used, "total": usage.total},
        "production": _production_sample(
            production_since_unix, production_mode, production_pause_run_id,
        ),
        "root_inode": int(stat.st_ino),
    }
