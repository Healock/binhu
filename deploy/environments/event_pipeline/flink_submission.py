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
from urllib.parse import urlsplit

from .identity import DEV_RUN_RE, STAGING_RUN_RE, environment_for_run_id
from .domain_pipeline import DomainPipelineOrchestrator
from .projection_definitions import BASE_PROJECTION_NAMES, projection_names


_DEFAULT_PLAN = DomainPipelineOrchestrator("development", "dev-contract").plan()
EXPECTED_SINKS = BASE_PROJECTION_NAMES | frozenset(_DEFAULT_PLAN.flink_sinks)
EXPECTED_SOURCES = frozenset(_DEFAULT_PLAN.flink_sources)
DUAL_TRACK_PREFIX_RE = re.compile(r"dev-[0-9]{8}-dualtrack-monitor(?:[A-Za-z0-9_-]*)")
DEV_TASK_READ_MODEL_RUN_RE = re.compile(r"^dev-[0-9]{8}-taskreadmodel[0-9]{2}$")
DEV_STALE_JOB_STATES = frozenset({"RUNNING", "RESTARTING"})
REST_TRANSIENT_CODES = frozenset({"flink_rest_transport_failed", "flink_rest_timeout"})
# Flink may accept the JAR before the JobGraph has finished planning and
# reached RUNNING.  A reset connection is therefore reconciled for the full
# startup window, rather than being reported as a failed submission after the
# shorter HTTP request timeout.  This covers the observed Flink 1.20 staging
# startup path while still leaving a finite bound for an actually lost job.
SUBMISSION_RECONCILIATION_TIMEOUT = 120.0
SAVEPOINT_ROOT = "/opt/flink/checkpoints/savepoints"
SAVEPOINT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")


class FlinkRestError(ValueError):
    """A bounded REST diagnostic that never carries response content."""

    def __init__(self, code: str, path: str, status: int | None = None) -> None:
        self.code = code
        self.path = path
        self.status = status
        super().__init__(code)

    def diagnostic(self) -> dict[str, Any]:
        result: dict[str, Any] = {"endpoint": self.path, "error_code": self.code}
        if self.status is not None:
            result["http_status"] = self.status
        return result


class FlinkRuntimeValidationError(ValueError):
    """A validation failure with a fixed stage, without exposing plan content."""

    def __init__(self, message: str, stage: str) -> None:
        self.stage = stage
        super().__init__(message)

    def diagnostic(self) -> dict[str, Any]:
        return {"stage": self.stage, "error_code": str(self)}


def savepoint_target(run_id: str, pause_id: str) -> str:
    """Return the only savepoint namespace accepted by the Dev controller."""
    environment_for_run_id(run_id)
    if not run_id.startswith("dev-"):
        raise ValueError("development savepoint run required")
    if not SAVEPOINT_ID_RE.fullmatch(pause_id or ""):
        raise ValueError("invalid Dev pause id")
    return f"file://{SAVEPOINT_ROOT}/{run_id}/{pause_id}"


def validate_savepoint_location(run_id: str, pause_id: str, value: object) -> str:
    expected = savepoint_target(run_id, pause_id)
    if not isinstance(value, str):
        raise FlinkRuntimeValidationError("Flink savepoint location mismatch", "savepoint_location")
    parsed = urlsplit(value)
    expected_path = expected.removeprefix("file://")
    if (parsed.scheme != "file" or parsed.netloc
            or not parsed.path.startswith(expected_path + "/savepoint-")
            or not re.fullmatch(r"/opt/flink/checkpoints/savepoints/[^/]+/[^/]+/savepoint-[A-Za-z0-9_-]+", parsed.path)):
        raise FlinkRuntimeValidationError("Flink savepoint location mismatch", "savepoint_location")
    return "file://" + parsed.path


def _one(pattern: str, text: str, label: str) -> str:
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    values = {value for value in matches if value}
    if len(values) != 1:
        raise ValueError(f"Flink {label} identity is ambiguous")
    return values.pop()


def parse_sql_identity(sql: str) -> dict[str, Any]:
    """Extract only the fixed identity fields needed for the environment fence."""
    if not isinstance(sql, str):
        raise ValueError("Flink SQL is not text")
    run_id = _one(r"pipeline\.name'\s*=\s*'([^']+)'", sql, "run_id")
    kafka_sources = {}
    for table, _columns, options in re.findall(
        r"CREATE\s+TABLE\s+([A-Za-z0-9_]+)\s*\((.*?)\)\s*WITH\s*\((.*?)\)\s*;",
        sql, flags=re.IGNORECASE | re.DOTALL,
    ):
        connector = _one(r"'connector'\s*=\s*'([^']+)'", options, "connector").lower()
        if connector != "kafka":
            continue
        group = _one(r"properties\.group\.id'\s*=\s*'([^']+)'", options, "consumer group")
        topic = _one(r"'topic'\s*=\s*'([^']+)'", options, "topic")
        if table in kafka_sources:
            raise ValueError("Flink Kafka source identity is duplicated")
        kafka_sources[table] = {"consumer_group": group, "topic": topic}
    filtered_runs = set(re.findall(r"run_id\s*=\s*'([^']+)'", sql, flags=re.IGNORECASE))
    environments = set(re.findall(r"environment\s*=\s*'([^']+)'", sql, flags=re.IGNORECASE))
    schema_versions = set(re.findall(r"schema_version\s*=\s*([0-9]+)", sql, flags=re.IGNORECASE))
    for match in re.findall(r"schema_version\s+IN\s*\(([^)]+)\)", sql, flags=re.IGNORECASE):
        schema_versions.update(re.findall(r"[0-9]+", match))
    if filtered_runs != {run_id}:
        raise ValueError("Flink SQL run_id filter does not match pipeline identity")
    environment = environment_for_run_id(run_id)
    if environments != {environment}:
        raise ValueError("Flink SQL environment filter does not match run identity")
    plan = DomainPipelineOrchestrator(environment, run_id).plan()
    expected_topics = {item["topic"] for item in plan.flink_sources.values()}
    if {item["topic"] for item in kafka_sources.values()} != expected_topics:
        raise ValueError("Flink Kafka topic does not match environment identity")
    if schema_versions != {str(version) for version in plan.flink_schema_versions}:
        raise ValueError("Flink SQL projection schema versions are incomplete")
    expected_sources = {
        name: {"consumer_group": value["consumer_group"], "topic": value["topic"]}
        for name, value in plan.flink_sources.items()
    }
    if kafka_sources != expected_sources:
        raise ValueError("Flink SQL consumer groups do not match manifest")
    sinks = frozenset(re.findall(
        r"INSERT\s+INTO\s+(dev_[A-Za-z0-9_]+)\b", sql, flags=re.IGNORECASE,
    ))
    allowed = projection_names()
    if not BASE_PROJECTION_NAMES <= sinks:
        missing = ",".join(sorted(BASE_PROJECTION_NAMES - sinks))
        raise ValueError(f"Flink SQL missing INSERT sink: {missing}")
    if not sinks <= allowed:
        unknown = ",".join(sorted(sinks - allowed))
        raise ValueError(f"Flink SQL contains unsupported INSERT sink: {unknown}")
    sinks = sinks | frozenset(plan.flink_sinks)
    return {"run_id": run_id,
            "consumer_groups": sorted(item["consumer_group"] for item in kafka_sources.values()),
            "topics": sorted({item["topic"] for item in kafka_sources.values()}),
            "sources": kafka_sources, "environment": environment, "sinks": sorted(sinks)}


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
    environment = environment_for_run_id(expected_run_id)
    identity = parse_sql_identity(sql)
    if identity["run_id"] != expected_run_id:
        raise ValueError("Flink SQL run_id does not match manifest")
    plan = DomainPipelineOrchestrator(environment, expected_run_id).plan()
    if set(identity["consumer_groups"]) != set(plan.consumer_groups):
        raise ValueError("Flink SQL consumer groups do not match manifest")
    return identity


def _job_descriptions(job: dict[str, Any]) -> list[str]:
    plan = job.get("plan") or {}
    nodes = plan.get("nodes", [])
    if not isinstance(nodes, list):
        return []
    return [str(node.get("description", "")) for node in nodes if isinstance(node, dict)]


def _named_operators(descriptions: list[str], kind: str) -> list[str]:
    return [
        match.group(1)
        for description in descriptions
        for match in re.finditer(
            rf"(?:^|\s){kind}:\s*([A-Za-z0-9_.-]+)(?=$|[\s\[(:])",
            description,
            flags=re.IGNORECASE,
        )
    ]


def validate_job_graph(
    job: dict[str, Any], expected_run_id: str,
    expected_sinks: Iterable[str] | None = None,
) -> dict[str, Any]:
    environment = environment_for_run_id(expected_run_id)
    if job.get("state") != "RUNNING":
        raise FlinkRuntimeValidationError("Flink job is not RUNNING", "job_state")
    # Flink 1.20's REST plan descriptions contain operator names and sink
    # labels, but do not reliably include SQL WHERE predicates.  The exact job
    # name is the stable run identity; SQL identity is checked before submit.
    if str(job.get("name", "")) != expected_run_id:
        raise FlinkRuntimeValidationError("Flink JobGraph run_id does not match", "job_name")
    descriptions = _job_descriptions(job)
    text = "\n".join(descriptions)
    # Table plans in Flink 1.20 do not always include connector options.  The
    # SQL identity fence and Kafka group check cover that case.  If a plan does
    # expose a topic marker, it must still be the fixed environment topic.
    topic_markers = set(re.findall(r"(?:topic|topics)\s*[=:]\s*['\"]?([A-Za-z0-9._-]+)", text, flags=re.IGNORECASE))
    plan = DomainPipelineOrchestrator(environment, expected_run_id).plan()
    expected_topics = {item["topic"] for item in plan.flink_sources.values()}
    if topic_markers and topic_markers != expected_topics:
        raise FlinkRuntimeValidationError("Flink JobGraph topic does not match environment identity", "job_topic")
    required = frozenset(expected_sinks or EXPECTED_SINKS)
    sources = _named_operators(descriptions, "Source")
    expected_sources = set(DomainPipelineOrchestrator(
        environment, expected_run_id,
    ).plan().flink_sources)
    if len(sources) != len(expected_sources) or set(sources) != expected_sources:
        raise FlinkRuntimeValidationError("Flink runtime Kafka sources do not match", "job_source")
    sinks = set(_named_operators(descriptions, "Sink"))
    if sinks - required:
        unexpected = ",".join(sorted(sinks - required))
        raise FlinkRuntimeValidationError(f"Flink runtime unexpected sink: {unexpected}", "job_sinks")
    if not required <= sinks:
        missing = ",".join(sorted(required - sinks))
        raise FlinkRuntimeValidationError(f"Flink runtime missing INSERT sink: {missing}", "job_sinks")
    return {"jid": job.get("jid"), "name": job.get("name"), "state": job["state"],
            "sinks": sorted(sinks)}


def partition_active_jobs(jobs: Iterable[dict[str, Any]], expected_run_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return current jobs and only stale jobs in known Dev/Staging pipeline families.

    Metadata jobs and unrelated Dev jobs are intentionally left out of the
    stale list.  Callers may cancel only the returned stale entries.
    """
    current: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    environment = environment_for_run_id(expected_run_id)
    for item in jobs:
        name = str(item.get("name", ""))
        if name == expected_run_id:
            current.append(item)
        elif environment == "development" and item.get("state") in DEV_STALE_JOB_STATES and (
            DUAL_TRACK_PREFIX_RE.search(name) or DEV_TASK_READ_MODEL_RUN_RE.fullmatch(name)
        ):
            stale.append(item)
        elif environment == "staging" and item.get("state") == "RUNNING" and STAGING_RUN_RE.search(name):
            stale.append(item)
    return current, stale


def validate_runtime(
    jobs: list[dict[str, Any]], consumer_groups: list[str], expected_run_id: str,
    expected_sinks: Iterable[str] | None = None,
) -> dict[str, Any]:
    if len(jobs) != 1:
        raise FlinkRuntimeValidationError("Flink runtime must have exactly one matching job", "job_count")
    required = frozenset(expected_sinks or EXPECTED_SINKS)
    job_reports = [validate_job_graph(item, expected_run_id, required) for item in jobs]
    sinks = {sink for report in job_reports for sink in report["sinks"]}
    if sinks - required:
        unexpected = ",".join(sorted(sinks - required))
        raise FlinkRuntimeValidationError(f"Flink runtime unexpected sink: {unexpected}", "job_sinks")
    if not required <= sinks:
        missing = ",".join(sorted(required - sinks))
        raise FlinkRuntimeValidationError(f"Flink runtime missing INSERT sink: {missing}", "job_sinks")
    plan = DomainPipelineOrchestrator(environment_for_run_id(expected_run_id), expected_run_id).plan()
    expected_groups = set(plan.consumer_groups)
    if set(consumer_groups) != expected_groups:
        raise FlinkRuntimeValidationError("Flink consumer group does not match current run", "consumer_group")
    return {"run_id": expected_run_id, "job_count": len(jobs),
            "consumer_groups": sorted(expected_groups),
            "job_ids": sorted(str(item.get("jid")) for item in jobs),
            "sinks": sorted(sinks)}


@dataclass
class FlinkRest:
    """Small injectable REST client; production use stays inside Dev JM."""

    container: str
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/") or ".." in path:
            raise ValueError("invalid Flink REST path")
        command = ["docker", "exec", self.container, "curl", "-sS", "--max-time", "15",
                   "-w", "\n__BINHU_HTTP_STATUS__:%{http_code}", "-X", method,
                   "http://127.0.0.1:8081" + path]
        if payload is not None:
            command += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(payload)]
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise FlinkRestError("flink_rest_timeout", path) from None
        except OSError:
            raise FlinkRestError("flink_rest_transport_failed", path) from None
        if result.returncode:
            raise FlinkRestError("flink_rest_transport_failed", path) from None
        marker = "\n__BINHU_HTTP_STATUS__:"
        body, separator, status_text = result.stdout.rpartition(marker)
        if not separator or not re.fullmatch(r"[0-9]{3}", status_text.strip()):
            raise FlinkRestError("flink_rest_response_invalid", path) from None
        status = int(status_text.strip())
        if not 200 <= status < 300:
            raise FlinkRestError(f"flink_rest_http_{status}", path, status) from None
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError:
            raise FlinkRestError("flink_rest_invalid_json", path, status) from None

    def overview(self) -> list[dict[str, Any]]:
        return list((self.request("/jobs/overview") or {}).get("jobs", []))

    def upload_jar(self, container_path: str) -> str:
        """Upload the JAR compiled from the current candidate source."""
        if container_path not in {
            "/tmp/dev-pipeline-job.jar",
            "/opt/flink/private/pipeline-job.jar",
            "/opt/flink/usrlib/dev-pipeline-job.jar",
        }:
            raise ValueError("unexpected Dev Flink JAR path")
        command = ["docker", "exec", self.container, "curl", "-sS", "--max-time", "30",
                   "-w", "\n__BINHU_HTTP_STATUS__:%{http_code}", "-X", "POST",
                   "http://127.0.0.1:8081/jars/upload",
                   "-F", "path=@" + container_path]
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            raise FlinkRestError("flink_rest_timeout", "/jars/upload") from None
        except OSError:
            raise FlinkRestError("flink_rest_transport_failed", "/jars/upload") from None
        if result.returncode:
            raise FlinkRestError("flink_rest_transport_failed", "/jars/upload") from None
        body, separator, status_text = result.stdout.rpartition("\n__BINHU_HTTP_STATUS__:")
        if not separator or not re.fullmatch(r"[0-9]{3}", status_text.strip()):
            raise FlinkRestError("flink_rest_response_invalid", "/jars/upload") from None
        status = int(status_text.strip())
        if not 200 <= status < 300:
            raise FlinkRestError(f"flink_rest_http_{status}", "/jars/upload", status) from None
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            raise FlinkRestError("flink_rest_invalid_json", "/jars/upload", status) from None
        filename = str(payload.get("filename", ""))
        # Flink returns filename as a path and status=success; use the final
        # path component as the stable JAR identifier for the run endpoint.
        if payload.get("status") != "success" or not filename:
            raise ValueError("Flink JAR upload was not accepted")
        return filename.rsplit("/", 1)[-1]

    def run_jar(self, jar_id: str, expected_run_id: str, *, savepoint_path: str | None = None) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.jar", jar_id or ""):
            raise ValueError("invalid Dev Flink JAR id")
        environment_for_run_id(expected_run_id)
        payload: dict[str, Any] = {
            "parallelism": 1,
            "entryClass": "PipelineJob",
            "programArgsList": [expected_run_id],
        }
        if savepoint_path is not None:
            match = re.fullmatch(
                rf"file://{re.escape(SAVEPOINT_ROOT)}/{re.escape(expected_run_id)}/([^/]+)/savepoint-[A-Za-z0-9_-]+",
                savepoint_path,
            )
            if not match or not SAVEPOINT_ID_RE.fullmatch(match.group(1)):
                raise ValueError("invalid Dev savepoint path")
            payload["savepointPath"] = savepoint_path
            payload["allowNonRestoredState"] = False
        response = self.request("/jars/" + jar_id + "/run", method="POST",
                                payload=payload)
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

    def trigger_savepoint(self, jid: str, target_directory: str) -> str:
        """Trigger a savepoint without cancelling the running job."""
        if not re.fullmatch(r"file:///opt/flink/checkpoints/savepoints/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", target_directory):
            raise ValueError("invalid Dev savepoint target")
        response = self.request(
            "/jobs/" + jid + "/savepoints", method="POST",
            payload={"target-directory": target_directory, "cancel-job": False},
        )
        trigger_id = str(response.get("request-id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]+", trigger_id):
            raise ValueError("Flink savepoint trigger id missing")
        return trigger_id

    def savepoint_status(self, jid: str, trigger_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", jid or "") and not re.fullmatch(r"jid-[A-Za-z0-9_-]+", jid or ""):
            raise ValueError("invalid Flink job id")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", trigger_id or ""):
            raise ValueError("invalid Flink savepoint trigger id")
        return dict(self.request("/jobs/" + jid + "/savepoints/" + trigger_id))


def submit_jar_or_reconcile(
    client: FlinkRest, jar_id: str, expected_run_id: str, *,
    timeout: float = SUBMISSION_RECONCILIATION_TIMEOUT,
) -> dict[str, Any]:
    """Submit once and reconcile an uncertain REST response without resubmitting.

    Flink can accept and start a JAR while the HTTP connection carrying the
    response is reset. A retry would create a duplicate JobGraph, so a
    transient submission error is reconciled by polling the fixed run name.
    """
    submission_error: FlinkRestError | None = None
    try:
        jid = client.run_jar(jar_id, expected_run_id)
        return {"mode": "submitted", "job_id": jid}
    except FlinkRestError as error:
        if error.code not in REST_TRANSIENT_CODES:
            raise
        submission_error = error
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            overview = client.overview()
        except FlinkRestError as probe_error:
            if probe_error.code not in REST_TRANSIENT_CODES:
                raise
            time.sleep(1)
            continue
        current, _ = partition_active_jobs(overview, expected_run_id)
        running = [item for item in current if item.get("state") == "RUNNING"]
        if len(running) == 1:
            return {"mode": "reconciled", "job_id": str(running[0].get("jid", ""))}
        time.sleep(1)
    assert submission_error is not None
    raise submission_error


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
        except FlinkRestError as exc:
            if exc.code not in REST_TRANSIENT_CODES:
                raise
        if time.monotonic() >= deadline:
            raise FlinkRestError("flink_rest_timeout", "/jobs/overview") from None
        time.sleep(1)


def wait_for_runtime(
    client: FlinkRest, expected_run_id: str,
    consumer_groups: Callable[[], list[str]], timeout: float = 120.0,
    expected_sinks: Iterable[str] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: ValueError | None = None
    last_transient: FlinkRestError | None = None
    while time.monotonic() < deadline:
        try:
            overview = client.overview()
        except FlinkRestError as exc:
            if exc.code not in REST_TRANSIENT_CODES:
                raise
            last_transient = exc
            time.sleep(2)
            continue
        current, _ = partition_active_jobs(overview, expected_run_id)
        if len(current) == 1:
            try:
                details = [client.details(str(item["jid"])) for item in current]
                return validate_runtime(
                    details, consumer_groups(), expected_run_id, expected_sinks,
                )
            except FlinkRestError as exc:
                if exc.code not in REST_TRANSIENT_CODES:
                    raise
                last_transient = exc
            except ValueError as exc:
                # Keep the first concrete validation error.  A later transient
                # REST failure must not erase the reason that blocked acceptance.
                if last_error is None:
                    last_error = exc
        time.sleep(2)
    if last_error is not None:
        raise last_error
    if last_transient is not None:
        raise last_transient
    raise FlinkRuntimeValidationError("Flink jobs not ready", "job_discovery")


def wait_for_savepoint(
    client: FlinkRest, jid: str, trigger_id: str, expected_location: str,
    timeout: float = 180.0,
) -> dict[str, str]:
    """Wait for a completed savepoint and fence its location to the Dev volume."""
    deadline = time.monotonic() + timeout
    last_status = ""
    while time.monotonic() < deadline:
        payload = client.savepoint_status(jid, trigger_id)
        status = payload.get("status")
        status_id = status.get("id") if isinstance(status, dict) else status
        last_status = str(status_id or "")
        if last_status == "COMPLETED":
            operation = payload.get("operation") or {}
            parsed = urlsplit(expected_location)
            run_id, pause_id = parsed.path.split("/")[-2:]
            location = validate_savepoint_location(run_id, pause_id, operation.get("location"))
            return {"trigger_id": trigger_id, "status": last_status, "location": location}
        if last_status in {"FAILED", "CANCELED"}:
            raise FlinkRuntimeValidationError("Flink savepoint did not complete", "savepoint")
        time.sleep(2)
    raise FlinkRuntimeValidationError("Flink savepoint timed out", "savepoint")


def wait_for_job_absent(client: FlinkRest, expected_run_id: str, timeout: float = 90.0) -> None:
    """Wait until the fixed Dev JobGraph has stopped before stopping its containers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current, _ = partition_active_jobs(client.overview(), expected_run_id)
        if not current:
            return
        time.sleep(1)
    raise FlinkRuntimeValidationError("Flink job did not stop", "job_stop")


def wait_for_stale_clear(client: FlinkRest, expected_run_id: str, timeout: float = 60.0) -> None:
    """Do not submit a new Dev graph while an old dual-track graph is cancelling."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, stale = partition_active_jobs(client.overview(), expected_run_id)
        if not stale:
            return
        time.sleep(1)
    raise ValueError("old Dev Flink dual-track jobs did not stop")
