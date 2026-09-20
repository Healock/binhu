"""Collect fixed, aggregate-only Staging load-test resource metrics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from .staging_compose import BROKERS, EVENT_TOPIC
from .staging_prepare import root_for


def _run(command: list[str], *, timeout: int = 30, input_text: str | None = None) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, input=input_text,
    )
    if result.returncode:
        raise RuntimeError("fixed Staging metrics command failed")
    return result.stdout


def _container(project: str, service: str) -> str:
    return f"{project}-{service}-1"


def _mysql_status(container: str) -> dict[str, int]:
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
    result: dict[str, int] = {}
    for line in output.splitlines():
        name, _, value = line.partition("\t")
        if name and value.strip().lstrip("-").isdigit():
            result[name] = int(value)
    required = {
        "Threads_connected", "Threads_running", "Innodb_row_lock_current_waits",
        "Innodb_row_lock_time_max", "Innodb_deadlocks",
    }
    if set(result) != required:
        raise RuntimeError("Staging MySQL metrics contract incomplete")
    return result


def _backend_pool(container: str, run_id: str) -> dict[str, int | float]:
    password = os.environ.get("STAGING_LOAD_TEST_PASSWORD", "")
    if len(password) < 16:
        raise RuntimeError("private Staging load-test password missing")
    script = r'''import http.cookies,json,sys,urllib.request
password=sys.stdin.readline().rstrip("\r\n")
login=urllib.request.Request(
    "http://127.0.0.1:37125/api/auth/login",
    data=json.dumps({"username":"staging-load-super_admin-01@staging","password":password,
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
    if not re.fullmatch(r"Staging_[A-Za-z0-9_]*OnlineData", database):
        raise RuntimeError("Staging OnlineData database identity invalid")
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
    output = _run([
        "docker", "exec", container, "sh", "-c",
        'REDISCLI_AUTH="$REDIS_PASSWORD" exec redis-cli --no-auth-warning INFO all',
    ])
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
    rows = 0
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
                rows += 1
                group_rows += 1
        if group_rows == 0:
            raise RuntimeError("Staging Kafka consumer metrics missing")
    if rows == 0:
        raise RuntimeError("Staging Kafka group metrics missing")
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


def sample(run_id: str) -> dict[str, Any]:
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
    database = ""
    for line in backend_env.read_text(encoding="utf-8").splitlines():
        if line.startswith("MYSQL_ONLINE_DATA_DB="):
            database = line.split("=", 1)[1]
            break
    pipeline_redis = _container(project, "staging-derived-redis")
    stat = Path(root).stat()
    disk = Path(root).anchor or "/"
    usage = __import__("shutil").disk_usage(disk)
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
        "derived_queue": _queue(backend, run_id, database),
        "containers": _containers(project),
        "disk": {"used": usage.used, "total": usage.total},
        # Production is deliberately absent: the isolated Staging gateway may
        # not inspect Production. A separately authorized read-only observer
        # must merge the Production sample before final verification.
        "production": None,
        "root_inode": int(stat.st_ino),
    }
