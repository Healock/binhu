"""Dev-only Flink submission and run-identity fences.

The Dev pipeline is deliberately submitted through the JobManager REST API
after Compose is healthy.  Configuration files on disk are not evidence that
the running JobGraph uses the same run; the JobGraph and Kafka group are checked
again before an apply is accepted.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .identity import DEV_RUN_RE, STAGING_RUN_RE, environment_for_run_id, topic_for


EXPECTED_SINKS = frozenset(("dev_revisions", "dev_task_metadata"))
DUAL_TRACK_PREFIX_RE = re.compile(r"dev-[0-9]{8}-dualtrack-monitor(?:[A-Za-z0-9_-]*)")


def _one(pattern: str, text: str, label: str) -> str:
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    values = {value for value in matches if value}
    if len(values) != 1:
        raise ValueError(f"Flink {label} identity is ambiguous")
    return values.pop()


def parse_sql_identity(sql: str) -> dict[str, str]:
    """Extract only the fixed identity fields needed for the environment fence."""
    if not isinstance(sql, str):
        raise ValueError("Flink SQL is not text")
    run_id = _one(r"pipeline\.name'\s*=\s*'([^']+)'", sql, "run_id")
    group = _one(r"properties\.group\.id'\s*=\s*'([^']+)'", sql, "consumer group")
    topic = _one(r"'topic'\s*=\s*'([^']+)'", sql, "topic")
    filtered_runs = set(re.findall(r"run_id\s*=\s*'([^']+)'", sql, flags=re.IGNORECASE))
    environments = set(re.findall(r"environment\s*=\s*'([^']+)'", sql, flags=re.IGNORECASE))
    if filtered_runs != {run_id}:
        raise ValueError("Flink SQL run_id filter does not match pipeline identity")
    environment = environment_for_run_id(run_id)
    if environments != {environment}:
        raise ValueError("Flink SQL environment filter does not match run identity")
    if topic != topic_for(environment):
        raise ValueError("Flink Kafka topic does not match environment identity")
    return {"run_id": run_id, "consumer_group": group, "topic": topic,
            "environment": environment}


def parse_runtime_identity(runtime: str) -> str:
    """Read the exact run id from the private runtime environment file."""
    values: dict[str, str] = {}
    for line in runtime.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in {"APP_ENVIRONMENT", "PIPELINE_RUN_ID", "DEV_RUN_ID", "STAGING_RUN_ID"}:
            values[key] = value
    environment = values.get("APP_ENVIRONMENT", "")
    run_id = values.get("PIPELINE_RUN_ID") or values.get("DEV_RUN_ID") or values.get("STAGING_RUN_ID", "")
    try:
        run_environment = environment_for_run_id(run_id)
    except ValueError:
        raise ValueError("runtime run_id is invalid") from None
    if run_environment != environment:
        raise ValueError(f"runtime environment is not {run_environment}")
    return run_id


def validate_sql_identity(sql: str, expected_run_id: str) -> dict[str, str]:
    environment_for_run_id(expected_run_id)
    identity = parse_sql_identity(sql)
    if identity["run_id"] != expected_run_id:
        raise ValueError("Flink SQL run_id does not match manifest")
    if identity["consumer_group"] != f"{expected_run_id}-flink":
        raise ValueError("Flink SQL consumer group does not match manifest")
    return identity


def _job_text(job: dict[str, Any]) -> str:
    plan = job.get("plan") or {}
    return "\n".join(str(node.get("description", "")) for node in plan.get("nodes", []))


def validate_job_graph(job: dict[str, Any], expected_run_id: str) -> dict[str, Any]:
    environment = environment_for_run_id(expected_run_id)
    if job.get("state") != "RUNNING":
        raise ValueError("Flink job is not RUNNING")
    if expected_run_id not in str(job.get("name", "")):
        raise ValueError("Flink JobGraph run_id does not match")
    text = _job_text(job)
    if expected_run_id not in text:
        raise ValueError("Flink JobGraph run_id filter does not match")
    if not re.search(r"environment\s*=\s*['\"]" + re.escape(environment) + r"['\"]", text, flags=re.IGNORECASE):
        raise ValueError("Flink JobGraph environment filter is missing")
    # Table plans in Flink 1.20 do not always include connector options.  The
    # SQL identity fence and Kafka group check cover that case; if a plan does
    # expose a topic marker, it must still be the fixed Dev topic.
    topic_markers = set(re.findall(r"(?:topic|topics)\s*[=:]\s*['\"]?([A-Za-z0-9._-]+)", text, flags=re.IGNORECASE))
    if topic_markers and topic_markers != {topic_for(environment)}:
        raise ValueError("Flink JobGraph topic does not match environment identity")
    sinks = {sink for sink in EXPECTED_SINKS if sink in text}
    if sinks != EXPECTED_SINKS:
        missing = ",".join(sorted(EXPECTED_SINKS - sinks))
        raise ValueError(f"Flink runtime missing INSERT sink: {missing}")
    return {"jid": job.get("jid"), "name": job.get("name"), "state": job["state"],
            "sinks": sorted(sinks)}


def partition_active_jobs(jobs: Iterable[dict[str, Any]], expected_run_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return current jobs and only stale jobs in the dual-track family.

    Metadata jobs and unrelated Dev jobs are intentionally left out of the
    stale list.  Callers may cancel only the returned stale entries.
    """
    current: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    environment = environment_for_run_id(expected_run_id)
    for item in jobs:
        name = str(item.get("name", ""))
        if expected_run_id in name:
            current.append(item)
        elif item.get("state") == "RUNNING" and (
            (environment == "development" and DUAL_TRACK_PREFIX_RE.search(name))
            or (environment == "staging" and STAGING_RUN_RE.search(name))
        ):
            stale.append(item)
    return current, stale


def validate_runtime(jobs: list[dict[str, Any]], consumer_groups: list[str], expected_run_id: str) -> dict[str, Any]:
    if len(jobs) != 1:
        raise ValueError("Flink runtime must have exactly one matching job")
    job_reports = [validate_job_graph(item, expected_run_id) for item in jobs]
    sinks = {sink for report in job_reports for sink in report["sinks"]}
    if sinks != EXPECTED_SINKS:
        missing = ",".join(sorted(EXPECTED_SINKS - sinks))
        raise ValueError(f"Flink runtime missing INSERT sink: {missing}")
    expected_group = f"{expected_run_id}-flink"
    if set(consumer_groups) != {expected_group}:
        raise ValueError("Flink consumer group does not match current run")
    return {"run_id": expected_run_id, "job_count": len(jobs),
            "consumer_group": expected_group,
            "job_ids": sorted(str(item.get("jid")) for item in jobs)}


@dataclass
class FlinkRest:
    """Small injectable REST client; production use stays inside Dev JM."""

    container: str
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/") or ".." in path:
            raise ValueError("invalid Flink REST path")
        command = ["docker", "exec", self.container, "curl", "-fsS", "--max-time", "15",
                   "-X", method, "http://127.0.0.1:8081" + path]
        if payload is not None:
            command += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(payload)]
        result = self.runner(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError("Flink REST request failed")
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {}

    def overview(self) -> list[dict[str, Any]]:
        return list((self.request("/jobs/overview") or {}).get("jobs", []))

    def upload_jar(self, container_path: str) -> str:
        """Upload the JAR compiled from the current candidate source."""
        if container_path not in {"/tmp/dev-pipeline-job.jar", "/opt/flink/private/pipeline-job.jar"}:
            raise ValueError("unexpected Dev Flink JAR path")
        command = ["docker", "exec", self.container, "curl", "-fsS", "--max-time", "30",
                   "-X", "POST", "http://127.0.0.1:8081/jars/upload",
                   "-F", "path=@" + container_path]
        result = self.runner(command, capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise ValueError("Flink JAR upload failed")
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            raise ValueError("Flink JAR upload response invalid") from None
        filename = str(payload.get("filename", ""))
        # Flink returns filename as a path and status=success; use the final
        # path component as the stable JAR identifier for the run endpoint.
        if payload.get("status") != "success" or not filename:
            raise ValueError("Flink JAR upload was not accepted")
        return filename.rsplit("/", 1)[-1]

    def run_jar(self, jar_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.jar", jar_id or ""):
            raise ValueError("invalid Dev Flink JAR id")
        response = self.request("/jars/" + jar_id + "/run", method="POST",
                                payload={"parallelism": 1, "entryClass": "PipelineJob"})
        jid = str(response.get("jobid", ""))
        if not re.fullmatch(r"[0-9a-f]{32}", jid):
            raise ValueError("Flink JAR submission returned no job id")
        return jid

    def details(self, jid: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", jid or "") and not re.fullmatch(r"jid-[A-Za-z0-9_-]+", jid or ""):
            raise ValueError("invalid Flink job id")
        return dict(self.request("/jobs/" + jid))

    def cancel(self, jid: str) -> None:
        self.request("/jobs/" + jid, method="PATCH")


def wait_for_rest(client: FlinkRest, timeout: float = 60.0) -> list[dict[str, Any]]:
    """Wait only for the JobManager REST listener to become reachable.

    Compose reports the JobManager container as started before Flink binds its
    REST port.  Retry that one fixed transport failure for a bounded period;
    all validation and identity errors remain immediate failures.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return client.overview()
        except ValueError as exc:
            if str(exc) != "Flink REST request failed":
                raise
        if time.monotonic() >= deadline:
            raise ValueError("Flink REST request failed") from None
        time.sleep(1)


def wait_for_runtime(client: FlinkRest, expected_run_id: str, consumer_groups: Callable[[], list[str]], timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = "Flink jobs not ready"
    while time.monotonic() < deadline:
        try:
            overview = client.overview()
        except ValueError as exc:
            if str(exc) != "Flink REST request failed":
                raise
            last_error = str(exc)
            time.sleep(2)
            continue
        current, _ = partition_active_jobs(overview, expected_run_id)
        if len(current) == 1:
            try:
                details = [client.details(str(item["jid"])) for item in current]
                return validate_runtime(details, consumer_groups(), expected_run_id)
            except ValueError as exc:
                last_error = str(exc)
        time.sleep(2)
    raise ValueError(last_error)


def wait_for_stale_clear(client: FlinkRest, expected_run_id: str, timeout: float = 60.0) -> None:
    """Do not submit a new Dev graph while an old dual-track graph is cancelling."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, stale = partition_active_jobs(client.overview(), expected_run_id)
        if not stale:
            return
        time.sleep(1)
    raise ValueError("old Dev Flink dual-track jobs did not stop")
