"""Six-hour, Dev-only operational observation for the metadata pipeline.

The observer is installed with the fixed Dev gateway and runs on the server.
It only addresses fixed container names from the three Dev Compose projects,
writes redacted evidence, and never connects to Production or Staging.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence


STATE = Path("/var/lib/binhu-dev-event-pipeline")
PROJECT = "binhu-development-pipeline"
EVIDENCE_VOLUME = f"{PROJECT}_evidence"
RUN_RE = re.compile(r"^dev-[0-9]{8}-[A-Za-z0-9][A-Za-z0-9_-]{3,31}$")
OBSERVATION_RE = re.compile(r"^obs-[0-9]{8}-[A-Za-z0-9][A-Za-z0-9_-]{3,31}$")
OBSERVATION_SECONDS = 6 * 60 * 60
SAMPLE_SECONDS = 30 * 60
SAMPLE_COUNT = OBSERVATION_SECONDS // SAMPLE_SECONDS + 1

PIPELINE_CONTAINERS = {
    "monitor": f"{PROJECT}-dual-track-monitor-1",
    "python_worker": f"{PROJECT}-python-metadata-worker-1",
    "relay": f"{PROJECT}-relay-1",
    "bridge": f"{PROJECT}-bridge-1",
    "redis": f"{PROJECT}-dev-derived-redis-1",
    "mysql": f"{PROJECT}-dev-derived-mysql-1",
}
KAFKA_CONTAINER = "binhu-development-eventbus-kafka-1-1"
KAFKA_GROUPS = "/opt/kafka/bin/kafka-consumer-groups.sh"
FLINK_CONTAINER = "binhu-development-flink-jobmanager-1"
OBSERVED_MEMORY = ("monitor", "python_worker", "relay")


REDIS_SCRIPT = r'''import asyncio,json,os
from redis.asyncio import Redis
async def main():
    client=Redis(host=os.environ["REDIS_HOST"],password=os.environ["REDIS_PASSWORD"],socket_timeout=5)
    try:
        memory=await client.info("memory")
        stats=await client.info("stats")
        errors=await client.info("errorstats")
        oom=errors.get("errorstat_OOM", {})
        if isinstance(oom, str):
            oom=dict(item.split("=",1) for item in oom.split(",") if "=" in item)
        print(json.dumps({"used_memory":int(memory.get("used_memory",0)),"maxmemory":int(memory.get("maxmemory",0)),"evicted_keys":int(stats.get("evicted_keys",0)),"oom_error_count":int((oom or {}).get("count",0))}))
    finally:
        await client.aclose()
asyncio.run(main())'''

MYSQL_SCRIPT = r'''import asyncio,json,os
import aiomysql
async def main():
    conn=await aiomysql.connect(host=os.environ["MYSQL_HOST"],user=os.environ["MYSQL_USER"],password=os.environ["MYSQL_PASSWORD"],db=os.environ["MYSQL_DATABASE"],connect_timeout=5,autocommit=True)
    try:
        async with conn.cursor() as cur:
            names=("Threads_connected","Threads_running","Max_used_connections","Innodb_row_lock_current_waits","Innodb_deadlocks")
            await cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ("+",".join(["%s"]*len(names))+")",names)
            values={str(k):int(v) for k,v in await cur.fetchall()}
            await cur.execute("SHOW GLOBAL VARIABLES LIKE 'max_connections'")
            row=await cur.fetchone(); values["max_connections"]=int(row[1])
            print(json.dumps(values))
    finally:
        conn.close()
asyncio.run(main())'''

FLINK_SCRIPT = r'''import json,os,urllib.request
def read(path):
    with urllib.request.urlopen("http://jobmanager:8081"+path,timeout=8) as response:
        return json.load(response)
run_id=os.environ["DEV_RUN_ID"]
jobs=[job for job in read("/jobs/overview").get("jobs",[]) if run_id in str(job.get("name",""))]
running=[job for job in jobs if job.get("state")=="RUNNING"]
if len(running)!=1: raise SystemExit(3)
job=running[0]; checkpoints=read("/jobs/"+job["jid"]+"/checkpoints")
counts=checkpoints.get("counts",{}); latest=(checkpoints.get("latest") or {}).get("completed") or {}
print(json.dumps({"job_count":len(jobs),"running_job_count":len(running),"checkpoint_completed":int(counts.get("completed",0)),"checkpoint_failed":int(counts.get("failed",0)),"checkpoint_in_progress":int(counts.get("in_progress",0)),"latest_completed_timestamp":int(latest.get("end_time",0))}))'''


class ObservationError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def checked(command: Sequence[str], *, timeout: int = 60) -> str:
    result = subprocess.run(list(command), capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ObservationError("fixed Dev observation command failed")
    return result.stdout


def _json_command(command: Sequence[str], *, timeout: int = 60) -> dict[str, Any]:
    try:
        value = json.loads(checked(command, timeout=timeout))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ObservationError("Dev observation returned invalid metrics") from None
    if not isinstance(value, dict):
        raise ObservationError("Dev observation returned invalid metrics")
    return value


def validate_identity(run_id: str, observation_id: str) -> None:
    if not RUN_RE.fullmatch(run_id or "") or not OBSERVATION_RE.fullmatch(observation_id or ""):
        raise ObservationError("invalid Dev observation identity")


def evidence_root(run_id: str) -> Path:
    raw = json.loads(checked(["docker", "volume", "inspect", EVIDENCE_VOLUME]))
    if not isinstance(raw, list) or len(raw) != 1:
        raise ObservationError("Dev evidence volume missing")
    item = raw[0]
    labels = item.get("Labels") or {}
    mountpoint = Path(str(item.get("Mountpoint", ""))).resolve()
    if (
        labels.get("com.docker.compose.project") != PROJECT
        or labels.get("binhu.environment") != "development"
        or not mountpoint.is_absolute()
        or not mountpoint.is_dir()
    ):
        raise ObservationError("Dev evidence volume identity mismatch")
    return mountpoint / run_id


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def _write_status(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _container_inspect(names: Iterable[str]) -> dict[str, dict[str, Any]]:
    items = json.loads(checked(["docker", "inspect", *names]))
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        name = str(item.get("Name", "")).lstrip("/")
        labels = (item.get("Config") or {}).get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        environment = labels.get("binhu.environment")
        if environment != "development" or project not in {
            PROJECT, "binhu-development-eventbus", "binhu-development-flink"
        }:
            raise ObservationError("non-Dev container rejected")
        state = item.get("State") or {}
        host = item.get("HostConfig") or {}
        log_config = host.get("LogConfig") or {}
        result[name] = {
            "status": state.get("Status"),
            "health": (state.get("Health") or {}).get("Status"),
            "restart_count": int(item.get("RestartCount", 0)),
            "oom_killed": bool(state.get("OOMKilled")),
            "memory_limit": int(host.get("Memory", 0)),
            "log_max_size": (log_config.get("Config") or {}).get("max-size"),
            "log_max_file": (log_config.get("Config") or {}).get("max-file"),
            "log_bytes": _log_bytes(Path(str(item.get("LogPath", "")))),
        }
    if set(result) != set(names):
        raise ObservationError("fixed Dev container set incomplete")
    return result


def _log_bytes(log_path: Path) -> int:
    if not log_path.is_absolute() or not log_path.name.endswith("-json.log"):
        raise ObservationError("Dev container log path invalid")
    total = 0
    for item in log_path.parent.glob(log_path.name + "*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


_MEMORY_UNITS = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3,
                 "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3}


def memory_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMG]?i?B)\s*", value, re.IGNORECASE)
    if not match:
        raise ObservationError("invalid Docker memory metric")
    return int(float(match.group(1)) * _MEMORY_UNITS[match.group(2).upper()])


def _docker_memory(names: Sequence[str]) -> dict[str, int]:
    lines = checked(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names]).splitlines()
    metrics: dict[str, int] = {}
    for line in lines:
        item = json.loads(line)
        name = str(item.get("Name", ""))
        used = str(item.get("MemUsage", "")).partition("/")[0].strip()
        metrics[name] = memory_bytes(used)
    if set(metrics) != set(names):
        raise ObservationError("Dev container memory metrics incomplete")
    return metrics


def _monitor_status(run_id: str) -> dict[str, Any]:
    path = f"/var/lib/binhu-dev-event-pipeline/evidence/{run_id}/monitor-status.json"
    status = _json_command(["docker", "exec", PIPELINE_CONTAINERS["monitor"], "cat", path])
    if (
        status.get("environment") != "development"
        or status.get("run_id") != run_id
        or status.get("status") != "running"
        or status.get("unattributed_difference_count") != 0
    ):
        raise ObservationError("Dev monitor heartbeat is not healthy")
    updated = datetime.fromisoformat(str(status.get("updated_at", "")).replace("Z", "+00:00"))
    age = (utc_now() - updated.astimezone(timezone.utc)).total_seconds()
    if not -5 <= age <= 60:
        raise ObservationError("Dev monitor heartbeat is stale")
    return {"updated_at": status["updated_at"], "age_seconds": round(age, 3),
            "unattributed_difference_count": 0}


def _kafka_lag(run_id: str) -> dict[str, Any]:
    group = run_id + "-flink"
    output = checked([
        "docker", "exec", KAFKA_CONTAINER, KAFKA_GROUPS,
        "--bootstrap-server", "kafka-1:9092", "--group", group, "--describe",
    ])
    lag = 0
    partitions = 0
    for raw in output.splitlines():
        line = raw.split()
        if not line or line[0] == "GROUP" or line[0].startswith("Consumer"):
            continue
        if line[0] != group or len(line) < 6:
            continue
        try:
            lag += int(line[5])
        except ValueError:
            raise ObservationError("Kafka lag output invalid") from None
        partitions += 1
    if partitions <= 0:
        raise ObservationError("Kafka consumer group missing")
    return {"group_sha256": hashlib.sha256(group.encode()).hexdigest(),
            "partitions": partitions, "lag": lag}


def _disk() -> dict[str, int]:
    target = Path("/data") if Path("/data").is_dir() else Path("/")
    usage = shutil.disk_usage(target)
    return {"total": usage.total, "used": usage.used, "free": usage.free,
            "used_percent": round(usage.used * 100 / usage.total, 3)}


def sample(run_id: str, sequence: int) -> dict[str, Any]:
    fixed_names = [*PIPELINE_CONTAINERS.values(), KAFKA_CONTAINER, FLINK_CONTAINER]
    inspected = _container_inspect(fixed_names)
    memory_names = [PIPELINE_CONTAINERS[name] for name in OBSERVED_MEMORY]
    memory = _docker_memory(memory_names)
    if any(item["status"] != "running" or item["oom_killed"] for item in inspected.values()):
        raise ObservationError("Dev component state invalid")
    return {
        "environment": "development",
        "run_id": run_id,
        "sequence": sequence,
        "sampled_at": iso(),
        "monitor": _monitor_status(run_id),
        "memory": {name: memory[PIPELINE_CONTAINERS[name]] for name in OBSERVED_MEMORY},
        "containers": inspected,
        "redis": _json_command(["docker", "exec", PIPELINE_CONTAINERS["bridge"],
                                "python", "-c", REDIS_SCRIPT]),
        "mysql": _json_command(["docker", "exec", PIPELINE_CONTAINERS["monitor"],
                                "python", "-c", MYSQL_SCRIPT]),
        "kafka": _kafka_lag(run_id),
        "flink": _json_command(["docker", "exec", PIPELINE_CONTAINERS["monitor"],
                                "python", "-c", FLINK_SCRIPT]),
        "disk": _disk(),
    }


def trigger_manual_tasks(run_id: str, observation_dir: Path) -> dict[str, Any]:
    manual_dir = f"/var/lib/binhu-dev-event-pipeline/evidence/{run_id}/{observation_dir.name}/manual-reconciliation"
    evidence_id = "dual-track-" + observation_dir.name.removeprefix("obs-")
    tasks = {
        "metadata_reconciliation": "passed",
        "schema_contract_reconciliation": "passed",
        "status_summary": "passed",
        "daily_report_refresh": "not_applicable_domain_not_migrated",
        "cleanup_task": "not_applicable_no_safe_manual_pipeline_task",
        "other_periodic_tasks": "not_applicable_current_domain",
    }
    checked(["docker", "exec", PIPELINE_CONTAINERS["monitor"], "python", "-m",
             "event_pipeline.dual_track_monitor", "--evidence-dir", manual_dir,
             "--evidence-id", evidence_id, "--cycles", "1"], timeout=180)
    checked(["docker", "exec", PIPELINE_CONTAINERS["bridge"], "python", "-m",
             "event_pipeline.schema_registry", "verify"], timeout=120)
    # Status aggregation is read-only, but execute it here so the manual-task
    # evidence proves that the monitor, Kafka group and Flink job were all
    # independently readable before the timed samples begin.
    status_summary = {
        "monitor": _monitor_status(run_id),
        "kafka": _kafka_lag(run_id),
        "flink": _json_command(["docker", "exec", PIPELINE_CONTAINERS["monitor"],
                                "python", "-c", FLINK_SCRIPT]),
    }
    return {"environment": "development", "run_id": run_id,
            "triggered_at": iso(), "tasks": tasks, "status_summary": status_summary}


def _slope_per_hour(values: Sequence[int]) -> float:
    count = len(values)
    xs = [index * SAMPLE_SECONDS / 3600 for index in range(count)]
    x_mean = sum(xs) / count
    y_mean = sum(values) / count
    denominator = sum((x - x_mean) ** 2 for x in xs)
    return 0.0 if denominator == 0 else sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / denominator


def evaluate(samples: Sequence[Mapping[str, Any]], manual_tasks: Mapping[str, Any]) -> dict[str, Any]:
    if len(samples) != SAMPLE_COUNT:
        raise ObservationError("six-hour sample set incomplete")
    reasons: list[str] = []
    trends: dict[str, Any] = {}
    for name in OBSERVED_MEMORY:
        values = [int(item["memory"][name]) for item in samples]
        limit = int(samples[-1]["containers"][PIPELINE_CONTAINERS[name]]["memory_limit"])
        slope = _slope_per_hour(values)
        allowed_delta = max(16 * 1024**2, int(limit * 0.10))
        allowed_slope = max(4 * 1024**2, limit * 0.02)
        passed = max(values) < limit * 0.90 and values[-1] - values[0] <= allowed_delta and slope <= allowed_slope
        trends[name] = {"first": values[0], "last": values[-1], "peak": max(values),
                        "slope_bytes_per_hour": round(slope, 3), "passed": passed}
        if not passed:
            reasons.append(name + "_memory_growth")
    redis_values = [int(item["redis"]["used_memory"]) for item in samples]
    redis_limit = int(samples[-1]["redis"]["maxmemory"])
    redis_slope = _slope_per_hour(redis_values)
    redis_oom = [int(item["redis"]["oom_error_count"]) for item in samples]
    redis_evictions = [int(item["redis"]["evicted_keys"]) for item in samples]
    redis_passed = (
        redis_limit > 0
        and max(redis_values) < redis_limit * 0.90
        and redis_values[-1] - redis_values[0] <= max(16 * 1024**2, int(redis_limit * 0.10))
        and redis_slope <= max(4 * 1024**2, redis_limit * 0.02)
        and redis_oom[-1] == redis_oom[0]
        and redis_evictions[-1] == redis_evictions[0]
    )
    trends["redis"] = {"first": redis_values[0], "last": redis_values[-1],
                       "peak": max(redis_values), "slope_bytes_per_hour": round(redis_slope, 3),
                       "first_oom_error_count": redis_oom[0],
                       "last_oom_error_count": redis_oom[-1],
                       "first_evicted_keys": redis_evictions[0],
                       "last_evicted_keys": redis_evictions[-1],
                       "passed": redis_passed}
    if not redis_passed:
        reasons.append("redis_capacity_or_growth")
    connections = [int(item["mysql"].get("Threads_connected", 0)) for item in samples]
    max_connections = int(samples[-1]["mysql"].get("max_connections", 0))
    mysql_passed = (
        max_connections > 0 and max(connections) <= max_connections * 0.80
        and connections[-1] <= connections[0] + 2
        and all(int(item["mysql"].get("Innodb_row_lock_current_waits", 0)) == 0 for item in samples)
        and int(samples[-1]["mysql"].get("Innodb_deadlocks", 0)) == int(samples[0]["mysql"].get("Innodb_deadlocks", 0))
    )
    trends["mysql"] = {"first_connections": connections[0], "last_connections": connections[-1],
                       "peak_connections": max(connections), "max_connections": max_connections,
                       "passed": mysql_passed}
    if not mysql_passed:
        reasons.append("mysql_connection_or_lock_trend")
    lag_values = [int(item["kafka"]["lag"]) for item in samples]
    kafka_passed = lag_values[-1] == 0 and all(value == 0 for value in lag_values[-3:])
    trends["kafka"] = {"first_lag": lag_values[0], "last_lag": lag_values[-1],
                       "peak_lag": max(lag_values), "passed": kafka_passed}
    if not kafka_passed:
        reasons.append("kafka_lag_not_converged")
    first_checkpoint = int(samples[0]["flink"]["checkpoint_completed"])
    last_checkpoint = int(samples[-1]["flink"]["checkpoint_completed"])
    first_failed = int(samples[0]["flink"]["checkpoint_failed"])
    last_failed = int(samples[-1]["flink"]["checkpoint_failed"])
    flink_passed = last_checkpoint >= first_checkpoint + 2 and last_failed == first_failed
    trends["flink"] = {"first_completed": first_checkpoint, "last_completed": last_checkpoint,
                       "first_failed": first_failed, "last_failed": last_failed,
                       "passed": flink_passed}
    if not flink_passed:
        reasons.append("flink_checkpoint_progress")
    disk_values = [int(item["disk"]["used"]) for item in samples]
    disk_passed = (samples[-1]["disk"]["used_percent"] < 85 and disk_values[-1] - disk_values[0] <= 1024**3)
    trends["disk"] = {"first_used": disk_values[0], "last_used": disk_values[-1],
                      "growth": disk_values[-1] - disk_values[0], "passed": disk_passed}
    if not disk_passed:
        reasons.append("disk_growth")
    if any(item["monitor"]["unattributed_difference_count"] != 0 for item in samples):
        reasons.append("unattributed_difference")
    container_trends: dict[str, Any] = {}
    container_names = set(samples[0]["containers"])
    if any(set(item["containers"]) != container_names for item in samples):
        reasons.append("container_set_changed")
    else:
        for name in sorted(container_names):
            values = [item["containers"][name] for item in samples]
            restarts = [int(value["restart_count"]) for value in values]
            restart_unchanged = all(value == restarts[0] for value in restarts)
            oom_free = not any(bool(value["oom_killed"]) for value in values)
            rotation_ok = all(
                value["log_max_size"] == "5m" and value["log_max_file"] == "2"
                for value in values
            )
            state_ok = all(value["status"] == "running" for value in values)
            health_ok = name != PIPELINE_CONTAINERS["monitor"] or all(
                value["health"] == "healthy" for value in values
            )
            container_trends[name] = {
                "first_restart_count": restarts[0],
                "last_restart_count": restarts[-1],
                "restart_unchanged": restart_unchanged,
                "oom_free": oom_free,
                "rotation_ok": rotation_ok,
                "state_ok": state_ok,
                "health_ok": health_ok,
            }
            if not restart_unchanged or not oom_free or not state_ok or not health_ok:
                reasons.append("container_restart_or_oom")
            if not rotation_ok:
                reasons.append("log_rotation_contract")
    trends["containers"] = container_trends
    expected_manual = {"metadata_reconciliation", "schema_contract_reconciliation", "status_summary"}
    task_values = manual_tasks.get("tasks") or {}
    if any(task_values.get(name) != "passed" for name in expected_manual):
        reasons.append("manual_periodic_task")
    reasons = sorted(set(reasons))
    return {"passed": not reasons, "failure_reasons": reasons, "trends": trends}


def run(run_id: str, observation_id: str) -> None:
    validate_identity(run_id, observation_id)
    directory = evidence_root(run_id) / observation_id
    if not directory.is_dir() or directory.is_symlink():
        raise ObservationError("Dev observation directory missing")
    status_path = directory / "observation-status.json"
    started = utc_now()
    manual: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []
    try:
        manual = trigger_manual_tasks(run_id, directory)
        _write_exclusive(directory / "manual-tasks.json", manual)
        for sequence in range(SAMPLE_COUNT):
            target = started + timedelta(seconds=sequence * SAMPLE_SECONDS)
            delay = (target - utc_now()).total_seconds()
            if delay > 0:
                time.sleep(delay)
            current = sample(run_id, sequence)
            samples.append(current)
            _write_exclusive(directory / f"sample-{sequence:02d}.json", current)
            _write_status(status_path, {"environment": "development", "run_id": run_id,
                "observation_id": observation_id, "status": "running", "started_at": iso(started),
                "expected_complete_at": iso(started + timedelta(seconds=OBSERVATION_SECONDS)),
                "samples_completed": len(samples), "samples_required": SAMPLE_COUNT,
                "unattributed_difference_count": 0})
        outcome = evaluate(samples, manual)
        report = {"environment": "development", "run_id": run_id,
                  "observation_id": observation_id, "started_at": iso(started),
                  "completed_at": iso(), "duration_seconds": OBSERVATION_SECONDS,
                  "sample_interval_seconds": SAMPLE_SECONDS, "sample_count": len(samples),
                  "manual_tasks": manual["tasks"], **outcome,
                  "unverified_non_blocking": [
                      "long_term_memory_leak", "certificate_renewal",
                      "kafka_seven_day_retention_expiry", "redis_natural_ttl_expiry",
                      "fixed_schedule_natural_trigger", "cross_day_state_accumulation",
                      "long_term_disk_growth",
                  ]}
        _write_exclusive(directory / "final-report.json", report)
        _write_status(status_path, {"environment": "development", "run_id": run_id,
            "observation_id": observation_id, "status": "passed" if outcome["passed"] else "failed",
            "completed_at": report["completed_at"], "samples_completed": len(samples),
            "samples_required": SAMPLE_COUNT, "failure_reasons": outcome["failure_reasons"]})
    except BaseException as error:
        _write_status(status_path, {"environment": "development", "run_id": run_id,
            "observation_id": observation_id, "status": "failed", "failed_at": iso(),
            "samples_completed": len(samples), "samples_required": SAMPLE_COUNT,
            "failure_reasons": [type(error).__name__]})
        raise


def start(run_id: str, observation_id: str) -> dict[str, Any]:
    validate_identity(run_id, observation_id)
    current_path = STATE / "current.json"
    if not current_path.is_file() or current_path.is_symlink():
        raise ObservationError("current Dev run missing")
    current = json.loads(current_path.read_text(encoding="utf-8"))
    if current.get("environment") != "development" or current.get("project") != PROJECT or current.get("run_id") != run_id:
        raise ObservationError("current Dev observation identity mismatch")
    directory = evidence_root(run_id) / observation_id
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    started = utc_now()
    initial = {"environment": "development", "run_id": run_id, "observation_id": observation_id,
               "status": "starting", "started_at": iso(started),
               "expected_complete_at": iso(started + timedelta(seconds=OBSERVATION_SECONDS)),
               "samples_completed": 0, "samples_required": SAMPLE_COUNT}
    _write_status(directory / "observation-status.json", initial)
    unit = "binhu-dev-observe-" + hashlib.sha256(observation_id.encode()).hexdigest()[:16]
    result = subprocess.run([
        "systemd-run", "--unit", unit, "--collect", "--property=Type=exec",
        "--property=NoNewPrivileges=true", sys.executable, str(Path(__file__).resolve()),
        "run", run_id, observation_id,
    ], capture_output=True, text=True, timeout=30)
    if result.returncode:
        shutil.rmtree(directory, ignore_errors=True)
        raise ObservationError("Dev observation service did not start")
    return initial


def status(run_id: str, observation_id: str) -> dict[str, Any]:
    validate_identity(run_id, observation_id)
    directory = evidence_root(run_id) / observation_id
    path = directory / "observation-status.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise ObservationError("Dev observation status missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("environment") != "development" or payload.get("run_id") != run_id or payload.get("observation_id") != observation_id:
        raise ObservationError("Dev observation status identity mismatch")
    return payload


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if action == "start" and len(sys.argv) == 4:
            print(json.dumps(start(sys.argv[2], sys.argv[3]), sort_keys=True))
        elif action == "status" and len(sys.argv) == 4:
            print(json.dumps(status(sys.argv[2], sys.argv[3]), sort_keys=True))
        elif action == "run" and len(sys.argv) == 4:
            run(sys.argv[2], sys.argv[3])
        else:
            raise ObservationError("fixed Dev observation command required")
    except (OSError, ValueError, KeyError, ObservationError, subprocess.TimeoutExpired):
        raise SystemExit("Dev six-hour observation refused; inspect private evidence") from None


if __name__ == "__main__":
    main()
