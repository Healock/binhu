"""Metrics collection and fail-safe stop rules for a shadow load-test run."""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import pymysql


GIB = 1024 ** 3
MIB = 1024 ** 2
CONTAINER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def summarize(csv_path: Path) -> dict[str, object]:
    if not csv_path.is_file():
        return {"requests": 0, "failures": 0, "success_rate": None, "rows": []}
    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows = [row for row in rows if row.get("Name") != "Aggregated"]
    requests = sum(int(row.get("Request Count") or 0) for row in rows)
    failures = sum(int(row.get("Failure Count") or 0) for row in rows)
    return {
        "requests": requests,
        "failures": failures,
        "success_rate": round((requests - failures) / requests, 6) if requests else None,
        "rows": rows,
    }


def _float(row: dict[str, str], *names: str) -> float:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


def locust_snapshot(prefix: Path) -> dict[str, Any]:
    summary = summarize(Path(f"{prefix}_stats.csv"))
    requests = int(summary["requests"] or 0)
    failures = int(summary["failures"] or 0)
    autosave_p95 = 0.0
    for row in summary["rows"]:
        if row.get("Name") == "PATCH mobile task save":
            autosave_p95 = _float(row, "95%", "95%ile")
            break
    return {
        "requests": requests,
        "failures": failures,
        "failure_rate": failures / requests if requests else 0.0,
        "autosave_p95_ms": autosave_p95,
    }


@dataclass(frozen=True)
class MonitorConfig:
    run_id: str
    compose_project: str
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    locust_prefix: Path
    artifact_dir: Path
    scenario: str = "mixed"
    production_health_url: str = ""
    production_containers: tuple[str, ...] = ()
    poll_seconds: int = 5


def _production_health(url: str) -> bool | None:
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def _production_container_states(names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for name in names:
        if not CONTAINER_RE.fullmatch(name):
            states[name] = {"error": "invalid_container_name"}
            continue
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{json .}}", name],
                capture_output=True, text=True, check=False,
            )
        except OSError:
            states[name] = {"error": "docker_unavailable"}
            continue
        if result.returncode:
            states[name] = {"error": "inspect_failed"}
            continue
        try:
            raw = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            states[name] = {"error": "invalid_inspect_output"}
            continue
        state = raw.get("State") or {}
        health = state.get("Health") or {}
        # docker inspect contains mounts, environment variables and command
        # arguments.  Monitoring only needs this explicit allowlist; never
        # persist the complete inspection payload in load-test artifacts.
        states[name] = {
            "running": bool(state.get("Running")),
            "restarting": bool(state.get("Restarting")),
            "oom_killed": bool(state.get("OOMKilled")),
            "health": str(health.get("Status") or ""),
            "restart_count": int(raw.get("RestartCount") or 0),
        }
    return states


def _shadow_database(config: MonitorConfig) -> dict[str, int]:
    connection = pymysql.connect(
        host=config.db_host, port=config.db_port, user=config.db_user,
        password=config.db_password, database=config.db_name,
        connect_timeout=3, read_timeout=3, write_timeout=3,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SHOW STATUS WHERE Variable_name IN ("
                "'Threads_connected','Threads_running',"
                "'Innodb_row_lock_current_waits','Innodb_row_lock_time_max')"
            )
            status = {
                str(row["Variable_name"]): int(row["Value"])
                for row in cursor.fetchall()
            }
            cursor.execute(
                "SELECT COALESCE(SUM(data_length+index_length),0) AS bytes "
                "FROM information_schema.tables WHERE table_schema=%s",
                (config.db_name,),
            )
            database_bytes = int(cursor.fetchone()["bytes"] or 0)
            cursor.execute(
                "SELECT "
                "COALESCE(SUM(status IN ('pending','retry')),0) AS queued,"
                "COALESCE(SUM(status='running'),0) AS running,"
                "COALESCE(SUM(status='failed'),0) AS failed,"
                "COALESCE(MAX(CASE WHEN status IN ('pending','retry') THEN "
                "TIMESTAMPDIFF(SECOND,created_at,UTC_TIMESTAMP()) END),0) AS oldest_wait,"
                "COALESCE(MAX(CASE WHEN status='running' THEN "
                "TIMESTAMPDIFF(SECOND,started_at,UTC_TIMESTAMP()) END),0) AS oldest_running "
                "FROM _online_projection_jobs"
            )
            queue = cursor.fetchone() or {}
            cursor.execute(
                "SELECT "
                "COALESCE(SUM(created_at>=DATE_SUB(UTC_TIMESTAMP(),INTERVAL 1 MINUTE)),0) AS enqueued_1m,"
                "COALESCE(SUM(finished_at>=DATE_SUB(UTC_TIMESTAMP(),INTERVAL 1 MINUTE) "
                "AND status IN ('succeeded','skipped')),0) AS processed_1m,"
                "COALESCE(SUM(error_code='coalesced_revision'),0) AS coalesced "
                "FROM _online_projection_jobs"
            )
            queue_rate = cursor.fetchone() or {}
    finally:
        connection.close()
    return {
        "connections": status.get("Threads_connected", 0),
        "running_threads": status.get("Threads_running", 0),
        "database_bytes": database_bytes,
        "innodb_row_lock_current_waits": status.get("Innodb_row_lock_current_waits", 0),
        "innodb_row_lock_time_max_ms": status.get("Innodb_row_lock_time_max", 0),
        "projection_queued": int(queue.get("queued") or 0),
        "projection_running": int(queue.get("running") or 0),
        "projection_failed": int(queue.get("failed") or 0),
        "projection_oldest_wait_seconds": int(queue.get("oldest_wait") or 0),
        "projection_oldest_running_seconds": int(queue.get("oldest_running") or 0),
        "projection_enqueue_rate_1m": int(queue_rate.get("enqueued_1m") or 0),
        "projection_process_rate_1m": int(queue_rate.get("processed_1m") or 0),
        "projection_coalesced": int(queue_rate.get("coalesced") or 0),
    }


def _shadow_container_stats(project: str) -> list[str]:
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return []
    if result.returncode:
        return []
    return [line for line in result.stdout.splitlines() if line.startswith(project)]


def _stop_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def projection_stop_reason(database: dict[str, Any]) -> str:
    if int(database.get("projection_failed", 0)) > 0:
        return "shadow_projection_job_failed"
    if int(database.get("projection_oldest_running_seconds", 0)) > 30:
        return "shadow_projection_job_stalled_above_30_seconds"
    if int(database.get("projection_oldest_wait_seconds", 0)) > 30:
        return "shadow_projection_queue_wait_above_30_seconds"
    return ""


def requests_stalled(
    *,
    scenario: str,
    request_count: int,
    last_progress_at: float,
    now: float,
) -> bool:
    return (
        scenario != "login"
        and request_count >= 100
        and now - last_progress_at > 30
    )


def monitor_process(process: subprocess.Popen, config: MonitorConfig) -> dict[str, Any]:
    """Watch the host and both environments; terminate Locust on a stop rule."""
    config.artifact_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = config.artifact_dir / f"{config.run_id}-metrics.jsonl"
    stop_path = config.artifact_dir / f"{config.locust_prefix.name}-stop-reason.json"
    baseline_swap = int(
        os.environ.get("SHADOW_BASELINE_SWAP_USED_BYTES") or psutil.swap_memory().used
    )
    health_failures = 0
    load_high_since: float | None = None
    iowait_high_since: float | None = None
    failure_high_since: float | None = None
    autosave_high_windows = 0
    last_request_count = 0
    last_request_progress_at = time.time()
    stop_reason = ""
    initial_states = _production_container_states(config.production_containers)
    baseline_restarts = {
        name: int(state.get("restart_count") or 0)
        for name, state in initial_states.items()
        if "error" not in state
    }

    while process.poll() is None:
        now = time.time()
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        cpu = psutil.cpu_times_percent(interval=None)
        iowait = float(getattr(cpu, "iowait", 0.0))
        try:
            load_1m = float(os.getloadavg()[0])
        except (AttributeError, OSError):
            load_1m = 0.0
        production_health = _production_health(config.production_health_url)
        if production_health is False:
            health_failures += 1
        elif production_health is True:
            health_failures = 0
        production_states = _production_container_states(config.production_containers)
        try:
            database = _shadow_database(config)
        except Exception as exc:
            database = {
                "connections": -1, "running_threads": -1, "database_bytes": -1,
                "error": type(exc).__name__,
            }
        locust = locust_snapshot(config.locust_prefix)
        request_count = int(locust["requests"] or 0)
        if request_count > last_request_count:
            last_request_count = request_count
            last_request_progress_at = now
        sample = {
            "at": now,
            "host": {
                "available_memory": memory.available,
                "swap_used": swap.used,
                "load_1m": load_1m,
                "iowait_percent": iowait,
            },
            "production_health": production_health,
            "production_containers": production_states,
            "shadow_database": database,
            "shadow_containers": _shadow_container_stats(config.compose_project),
            "locust": locust,
        }
        with metrics_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n"
            )

        if memory.available < 4 * GIB:
            stop_reason = "host_available_memory_below_4gib"
        elif swap.used - baseline_swap > 256 * MIB:
            stop_reason = "host_swap_growth_above_256mib"
        elif health_failures >= 2:
            stop_reason = "production_health_failed_twice"
        elif any("error" in state for state in production_states.values()):
            stop_reason = "production_container_inspection_failed"
        elif any(
            state.get("oom_killed")
            or state.get("restarting")
            or not state.get("running", False)
            or state.get("health") == "unhealthy"
            or int(state.get("restart_count") or 0) > baseline_restarts.get(name, 0)
            for name, state in production_states.items()
        ):
            stop_reason = "production_container_unhealthy_or_restarted"
        elif database.get("error"):
            stop_reason = "shadow_database_monitor_failed"
        elif int(database.get("connections", -1)) > 120:
            stop_reason = "shadow_mysql_connections_above_120"
        elif int(database.get("running_threads", -1)) > 30:
            stop_reason = "shadow_mysql_running_threads_above_30"
        elif int(database.get("database_bytes", -1)) >= 10 * GIB:
            stop_reason = "shadow_database_size_reached_10gib"
        else:
            stop_reason = projection_stop_reason(database)
        if load_1m > 28:
            load_high_since = load_high_since or now
        else:
            load_high_since = None
        if iowait > 20:
            iowait_high_since = iowait_high_since or now
        else:
            iowait_high_since = None
        if locust["requests"] >= 100 and locust["failure_rate"] > 0.02:
            failure_high_since = failure_high_since or now
        else:
            failure_high_since = None
        if locust["autosave_p95_ms"] > 3000:
            autosave_high_windows += 1
        else:
            autosave_high_windows = 0
        if not stop_reason and load_high_since and now - load_high_since >= 60:
            stop_reason = "host_load_above_28_for_60_seconds"
        if not stop_reason and iowait_high_since and now - iowait_high_since >= 60:
            stop_reason = "host_iowait_above_20_percent_for_60_seconds"
        if not stop_reason and failure_high_since and now - failure_high_since >= 60:
            stop_reason = "shadow_unexpected_failure_rate_above_2_percent_for_60_seconds"
        if not stop_reason and autosave_high_windows >= 2:
            stop_reason = "autosave_p95_above_3_seconds_for_two_windows"
        if not stop_reason and requests_stalled(
            scenario=config.scenario,
            request_count=request_count,
            last_progress_at=last_request_progress_at,
            now=now,
        ):
            stop_reason = "shadow_requests_stalled_for_30_seconds"

        if stop_reason:
            _stop_process(process)
            payload = {
                "run_id": config.run_id,
                "stopped": True,
                "reason": stop_reason,
                "sample": sample,
            }
            stop_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return payload
        time.sleep(config.poll_seconds)

    payload = {
        "run_id": config.run_id,
        "stopped": False,
        "returncode": process.returncode,
    }
    stop_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("用法：python metrics.py <locust_stats.csv>")
    print(json.dumps(summarize(Path(sys.argv[1])), ensure_ascii=False, indent=2))
