"""Measured, hash-fenced startup of the explicitly prepared Dev-only project."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from .prepare import ROOT, PROJECT, NETWORK, checked, compose
from . import flink_compose, flink_submission
from .identity import summary_topic_for
from .services.kafka_envelope import EVENT_TOPIC, DLQ_TOPIC


FLINK_COMPOSE = Path("/srv/binhu-environments/development-eventbus/flink-pipeline-compose.json")
FLINK_JOBMANAGER = "binhu-development-flink-jobmanager-1"
KAFKA_CONTAINER = "binhu-development-eventbus-kafka-1-1"
FLINK_CANDIDATE_JAR = "/tmp/dev-pipeline-job.jar"
_SAFE_CONTROL_FAILURES = frozenset(
    {
        "unexpected Dev root",
        "manifest identity mismatch",
        "runtime file identity mismatch",
        "runtime differs from closed Dev resource definition",
        "Dev event network identity mismatch",
        "foreign network dependency",
        "Dev volume referenced by another project",
        "pipeline container has unexpected network",
        "insufficient memory reserve",
        "Dev resource inspection failed",
        "Dev Flink runtime command failed",
        "Dev Flink consumer group has no task topic assignment",
        "Dev runtime.env run_id does not match manifest",
        "current Dev PipelineJob.java is missing",
        "Dev Flink Compose definition is missing",
        "Dev Flink Compose definition is invalid",
        "Dev Flink Compose project identity mismatch",
        "Dev Flink Compose network identity mismatch",
        "Dev Flink jobmanager environment label missing",
        "Dev Flink taskmanager environment label missing",
        "Dev Flink jobmanager pids_limit missing",
        "Dev Flink taskmanager pids_limit missing",
        "Dev Flink jobmanager checkpoint volume missing",
        "Dev Flink taskmanager checkpoint volume missing",
        "Dev Flink task runtime env file missing",
        "Dev Flink task runtime env file identity mismatch",
        "Dev Flink Backend network identity mismatch",
        "Dev Flink Backend network attachment mismatch",
        "Dev Flink task runtime environment is unavailable",
        "Dev Flink task runtime environment is invalid",
        "Dev Flink task runtime identity mismatch",
        "Dev Flink task Backend Redis identity mismatch",
        "Dev Flink artifact identity mismatch",
        "Dev Flink current run has an incomplete job set",
        "register the fixed Dev schema before starting workers",
        "Dev Schema Registry registration timed out",
        "startup deadline reached; preserve resources and remeasure",
        "startup failed; preserve private diagnostics",
        "Dev delivery index migration failed",
        "Flink jobs not ready",
        "old Dev Flink dual-track jobs did not stop",
        "Flink job is not RUNNING",
        "Flink JobGraph run_id does not match",
        "Flink JobGraph run_id filter does not match",
        "Flink JobGraph environment filter is missing",
        "Flink JobGraph topic is not the fixed Dev topic",
        "Flink runtime must have exactly one matching job",
        "Flink consumer group does not match current run",
        "Flink REST request failed",
        "flink_rest_transport_failed",
        "flink_rest_timeout",
        "flink_rest_response_invalid",
        "flink_rest_invalid_json",
        "Flink JAR upload failed",
        "Flink JAR upload response invalid",
        "Flink JAR upload was not accepted",
        "Flink JAR submission returned no job id",
        "current Dev PipelineJob compilation failed",
        "evidence directory initialization failed",
        "Flink connector identity is ambiguous",
        "Flink consumer group identity is ambiguous",
        "Flink Kafka source identity is duplicated",
        "Flink topic identity is ambiguous",
        "Flink SQL is not text",
        "Flink SQL run_id filter does not match pipeline identity",
        "Flink SQL environment filter does not match run identity",
        "Flink Kafka topic does not match environment identity",
        "Flink SQL task/view-state schema versions are incomplete",
        "Flink SQL consumer groups do not match manifest",
        "Flink SQL run_id does not match manifest",
    }
)


def safe_control_failure_detail(error: Exception) -> str:
    """Return only a fixed, non-sensitive Dev control gate reason."""
    detail = str(error)
    if detail in _SAFE_CONTROL_FAILURES or re.fullmatch(
        r"Flink runtime (?:missing INSERT sink|unexpected sink): [A-Za-z0-9_,.-]+", detail
    ) or re.fullmatch(
        r"Flink SQL (?:missing INSERT sink|contains unsupported INSERT sink): [A-Za-z0-9_,.-]+",
        detail,
    ) or re.fullmatch(r"flink_rest_http_[0-9]{3}", detail):
        return detail
    return type(error).__name__


def expected_networks(service):
    """Return the only networks allowed for a Dev pipeline service."""
    if service == "business-bridge":
        return {NETWORK, "binhu-development_internal"}
    if service == "backend-outbox-relay":
        return {"binhu-development_internal"}
    if service == "taskmanager":
        return {NETWORK, "binhu-development_internal"}
    return {NETWORK}


def evidence_initializer_command(run_id: str) -> list[str]:
    """Build the one-shot command that grants the monitor its run directory.

    The evidence volume is intentionally persistent and may have been created
    by Docker as root.  Only the current run directory is created/chowned;
    existing evidence and checkpoint volumes are never touched.
    """
    if not re.fullmatch(r"dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id or ""):
        raise ValueError("evidence directory initialization failed")
    script = (
        "import os; from pathlib import Path; "
        f"p=Path('/var/lib/binhu-dev-event-pipeline/evidence') / {run_id!r}; "
        "p.mkdir(parents=True, exist_ok=True); os.chown(p, 10001, 10001); "
        "os.chmod(p, 0o700)"
    )
    return [
        "docker", "compose", "-f", (ROOT / "compose.json").as_posix(),
        "run", "--rm", "--no-deps", "--user", "0:0", "dual-track-monitor",
        "python", "-c", script,
    ]


def _prepare_evidence_directory(run_id: str) -> None:
    result = subprocess.run(
        evidence_initializer_command(run_id), capture_output=True, text=True, timeout=45
    )
    if result.returncode:
        raise ValueError("evidence directory initialization failed")


def measure():
    if ROOT.is_symlink() or ROOT.parent.is_symlink() or ROOT.resolve() != ROOT:
        raise ValueError("unexpected Dev root")
    manifest = json.loads((ROOT / "manifest.json").read_text())
    if manifest.get("environment") != "development" or manifest.get("project") != PROJECT:
        raise ValueError("manifest identity mismatch")
    for name, digest in manifest["hashes"].items():
        path = ROOT / name
        if path.is_symlink() or path.parent != ROOT or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("runtime file identity mismatch")
    spec = json.loads((ROOT / "compose.json").read_text())
    if spec != compose(manifest["images"]):
        raise ValueError("runtime differs from closed Dev resource definition")
    network = json.loads(checked(["docker", "network", "inspect", NETWORK]))[0]
    if not network.get("Internal") or network.get("Labels", {}).get("com.docker.compose.project") != "binhu-development-eventbus":
        raise ValueError("Dev event network identity mismatch")
    for info in network.get("Containers", {}).values():
        if not info["Name"].startswith("binhu-development-"):
            raise ValueError("foreign network dependency")
    all_ids = checked(["docker", "ps", "-aq"]).split()
    containers = json.loads(checked(["docker", "inspect", *all_ids])) if all_ids else []
    for item in containers:
        project = item["Config"].get("Labels", {}).get("com.docker.compose.project")
        for mount in item.get("Mounts", []):
            if mount.get("Name", "").startswith(PROJECT + "_") and project != PROJECT:
                raise ValueError("Dev volume referenced by another project")
        if project == PROJECT:
            service = item["Config"].get("Labels", {}).get("com.docker.compose.service")
            if set(item["NetworkSettings"]["Networks"]) != expected_networks(service):
                raise ValueError("pipeline container has unexpected network")
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(memory["MemAvailable"].split()[0])
    if available < 2 * 1024**2:
        raise ValueError("insufficient memory reserve")
    checked(["docker", "compose", "-f", str(ROOT / "compose.json"), "config", "--quiet"])
    return {"environment": "development", "project": PROJECT, "run_id": manifest["run_id"],
            "hashes_verified": True, "isolation_verified": True, "memory_available_kib": available}


def _run_checked(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError("Dev Flink runtime command failed")
    return result


def _consumer_groups(expected_groups: list[str]) -> list[str]:
    expected = set(expected_groups)
    run_id = expected_groups[0].removesuffix("-flink") if expected_groups else ""
    canonical = {f"{run_id}-flink", f"{run_id}-flink-redis"}
    if expected != canonical:
        raise ValueError("Dev Flink consumer groups do not match current run")
    topic = flink_submission.topic_for("development")
    for expected_group in sorted(expected):
        result = _run_checked([
            "docker", "exec", KAFKA_CONTAINER,
            "/opt/kafka/bin/kafka-consumer-groups.sh",
            "--bootstrap-server", "kafka-1:9092",
            "--describe", "--group", expected_group,
        ])
        assignments = {
            parts[1]
            for line in result.stdout.splitlines()
            if (parts := line.split()) and len(parts) >= 2 and parts[0] == expected_group
        }
        if assignments and assignments != {topic}:
            raise ValueError("Dev Flink consumer group has no task topic assignment")
    return sorted(expected)


def _validate_flink_sql(manifest: dict) -> dict:
    run_id = manifest["run_id"]
    sql = (ROOT / "pipeline.sql").read_text(encoding="utf-8")
    identity = flink_submission.validate_sql_identity(sql, run_id)
    runtime = (ROOT / "runtime.env").read_text(encoding="utf-8")
    if flink_submission.parse_runtime_identity(runtime) != run_id:
        raise ValueError("Dev runtime.env run_id does not match manifest")
    source = Path(__file__).with_name("PipelineJob.java")
    if source.is_symlink() or not source.is_file():
        raise ValueError("current Dev PipelineJob.java is missing")
    return identity


def _validate_flink_compose() -> None:
    """Check the fixed Flink Compose identity before touching containers."""
    if FLINK_COMPOSE.is_symlink() or not FLINK_COMPOSE.is_file():
        raise ValueError("Dev Flink Compose definition is missing")
    try:
        spec = json.loads(FLINK_COMPOSE.read_text(encoding="utf-8"))
        services = spec["services"]
        network = spec["networks"]["internal"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Dev Flink Compose definition is invalid") from exc
    if spec.get("name") != "binhu-development-flink" or set(services) != {"jobmanager", "taskmanager"}:
        raise ValueError("Dev Flink Compose project identity mismatch")
    if network.get("name") != "binhu-development-eventbus_internal" or not network.get("external"):
        raise ValueError("Dev Flink Compose network identity mismatch")
    for name, service in services.items():
        if service.get("labels", {}).get("binhu.environment") != "development":
            raise ValueError(f"Dev Flink {name} environment label missing")
        if service.get("pids_limit") != 256:
            raise ValueError(f"Dev Flink {name} pids_limit missing")
        if service.get("restart") != "on-failure:3":
            raise ValueError(f"Dev Flink {name} restart policy mismatch")
        expected_memory = "2147483648" if name == "taskmanager" else "805306368"
        if service.get("mem_limit") != expected_memory:
            raise ValueError(f"Dev Flink {name} memory limit mismatch")
        if name == "taskmanager" and "taskmanager.memory.process.size: 1792m" not in service.get(
            "environment", {}
        ).get("FLINK_PROPERTIES", ""):
            raise ValueError("Dev Flink taskmanager process memory limit mismatch")
        mounts = service.get("volumes", [])
        if not any(mount.get("target") == "/opt/flink/checkpoints" and mount.get("source") == "flink-checkpoints"
                   for mount in mounts if isinstance(mount, dict)):
            raise ValueError(f"Dev Flink {name} checkpoint volume missing")
    if services["taskmanager"].get("env_file") != [str(flink_compose.PIPELINE_TASK_ENV)]:
        raise ValueError("Dev Flink task runtime env file missing")


def _flink_artifact_root() -> Path:
    spec = json.loads(FLINK_COMPOSE.read_text(encoding="utf-8"))
    mounts = spec["services"]["taskmanager"].get("volumes", [])
    target = "/opt/flink/usrlib/dev-pipeline-job.jar"
    sources = [item.get("source", "") for item in mounts
               if item.get("target") == target and item.get("type") == "bind"]
    if len(sources) != 1:
        raise ValueError("Dev Flink artifact identity mismatch")
    artifact = Path(sources[0])
    if artifact.name != "dev-pipeline-job.jar":
        raise ValueError("Dev Flink artifact identity mismatch")
    return artifact.parent


def _validate_flink_task_runtime_environment(manifest: dict) -> None:
    path = ROOT / "flink-task.env"
    if (path.is_symlink() or not path.is_file()
            or (sys.platform != "win32" and path.stat().st_mode & 0o077)):
        raise ValueError("Dev Flink task runtime environment is unavailable")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise ValueError("Dev Flink task runtime environment is invalid")
        values[key] = value
    if (values.get("APP_ENVIRONMENT") != "development"
            or values.get("DEV_RUN_ID") != manifest.get("run_id")
            or len(values.get("TASK_EVENT_ENCRYPTION_KEY", "").encode("utf-8")) < 32):
        raise ValueError("Dev Flink task runtime identity mismatch")
    try:
        redis = urlsplit(values.get("BACKEND_REDIS_URL", ""))
        redis_port = redis.port
    except ValueError:
        raise ValueError("Dev Flink task Backend Redis identity mismatch") from None
    if (redis.scheme != "redis" or redis.hostname not in {"redis", "environment-redis"}
            or redis_port != 6379 or redis.path != "/0" or not redis.password):
        raise ValueError("Dev Flink task Backend Redis identity mismatch")


def _ensure_flink_task_runtime_compose(manifest: dict, evidence: Path) -> dict | None:
    _validate_flink_task_runtime_environment(manifest)
    artifact_root = _flink_artifact_root()
    image = manifest["images"]["flink"]
    report = flink_compose.measure(image, artifact_root)
    if not report["requires_update"]:
        return None
    evidence_id = "dev-flink-runtime-" + hashlib.sha256(
        manifest["run_id"].encode("utf-8")
    ).hexdigest()[:16]
    updated = flink_compose.apply(image, artifact_root, evidence_id)
    audit = evidence / "flink-compose-runtime.json"
    audit.write_text(json.dumps(updated, sort_keys=True) + "\n", encoding="utf-8")
    audit.chmod(0o600)
    return updated


def _build_candidate_flink_jar(source: Path) -> dict[str, str]:
    """Copy the immutable JAR built from the checked candidate source."""
    if source.is_symlink() or not source.is_file() or source.name != "PipelineJob.java":
        raise ValueError("current Dev PipelineJob.java is missing")
    jar_source = source.parents[3] / "pipeline-job.jar"
    if jar_source.is_symlink() or not jar_source.is_file():
        raise ValueError("current Dev PipelineJob compilation failed")
    _run_checked(["docker", "cp", str(jar_source), f"{FLINK_JOBMANAGER}:{FLINK_CANDIDATE_JAR}"])
    digest = _run_checked([
        "docker", "exec", FLINK_JOBMANAGER, "sha256sum", FLINK_CANDIDATE_JAR
    ]).stdout.split(maxsplit=1)[0]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("current Dev PipelineJob compilation failed")
    return {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "jar_sha256": digest,
        "container_path": FLINK_CANDIDATE_JAR,
    }


def _submit_and_verify_flink(manifest: dict, evidence: Path) -> dict:
    """Submit the current Dev JobGraph and fence its run identity."""
    identity = _validate_flink_sql(manifest)
    _ensure_flink_task_runtime_compose(manifest, evidence)
    _validate_flink_compose()
    _run_checked(["docker", "compose", "-f", str(FLINK_COMPOSE), "up", "-d", "jobmanager", "taskmanager"], timeout=180)
    source = Path(__file__).with_name("PipelineJob.java")
    candidate_jar = _build_candidate_flink_jar(source)
    client = flink_submission.FlinkRest(FLINK_JOBMANAGER)
    overview = flink_submission.wait_for_rest(client)
    current, stale = flink_submission.partition_active_jobs(overview, manifest["run_id"])
    stale_summary = [{"jid": item.get("jid"), "name": item.get("name"), "state": item.get("state")}
                     for item in stale]
    stale_path = evidence / "flink-stale-jobs.json"
    stale_path.write_text(json.dumps({"environment": "development", "run_id": manifest["run_id"],
                                      "jobs": stale_summary}, indent=2) + "\n", encoding="utf-8")
    stale_path.chmod(0o600)
    for item in stale:
        jid = str(item.get("jid", ""))
        if jid:
            client.cancel(jid)
    if stale:
        flink_submission.wait_for_stale_clear(client, manifest["run_id"])
    if len(current) not in (0, 1):
        raise ValueError("Dev Flink current run has an incomplete job set")
    if len(current) == 0:
        jar_id = client.upload_jar(candidate_jar["container_path"])
        client.run_jar(jar_id, manifest["run_id"])
    verified = flink_submission.wait_for_runtime(
        client, manifest["run_id"], lambda: _consumer_groups(identity["consumer_groups"]),
        expected_sinks=identity.get("sinks", flink_submission.EXPECTED_SINKS),
    )
    runtime_path = evidence / "flink-runtime.json"
    runtime_path.write_text(json.dumps({**verified, "identity": identity,
                                        "candidate_jar": candidate_jar}, indent=2) + "\n", encoding="utf-8")
    runtime_path.chmod(0o600)
    return verified


def _ensure_task_topics() -> list[str]:
    topics = (EVENT_TOPIC, DLQ_TOPIC)
    for topic in topics:
        _run_checked([
            "docker", "exec", KAFKA_CONTAINER, "/opt/kafka/bin/kafka-topics.sh",
            "--bootstrap-server", "kafka-1:9092", "--create", "--if-not-exists",
            "--topic", topic, "--partitions", "3", "--replication-factor", "3",
            "--config", "min.insync.replicas=2",
        ])
    description = _run_checked([
        "docker", "exec", KAFKA_CONTAINER, "/opt/kafka/bin/kafka-topics.sh",
        "--bootstrap-server", "kafka-1:9092", "--describe",
    ]).stdout
    if any(topic not in description for topic in topics):
        raise ValueError("Dev task topic verification failed")
    return list(topics)


def _ensure_summary_topics(environment: str) -> list[str]:
    """Create only the fixed non-Production online-summary topics."""
    topics = (summary_topic_for(environment), summary_topic_for(environment, dead_letter=True))
    for topic in topics:
        _run_checked([
            "docker", "exec", KAFKA_CONTAINER, "/opt/kafka/bin/kafka-topics.sh",
            "--bootstrap-server", "kafka-1:9092", "--create", "--if-not-exists",
            "--topic", topic, "--partitions", "3", "--replication-factor", "3",
            "--config", "min.insync.replicas=2",
        ])
    description = _run_checked([
        "docker", "exec", KAFKA_CONTAINER, "/opt/kafka/bin/kafka-topics.sh",
        "--bootstrap-server", "kafka-1:9092", "--describe",
    ]).stdout
    if any(topic not in description for topic in topics):
        raise ValueError("Dev online-summary topic verification failed")
    return list(topics)


def apply():
    report = measure()
    # Atomically allocate a private directory even if the clock repeats or
    # concurrent attempts start within the same platform clock tick.
    evidence = Path(tempfile.mkdtemp(prefix="apply-evidence-", dir=ROOT))
    # ``measure`` always returns run_id in production.  Keeping the guard
    # makes the helper straightforward to exercise in isolated unit tests
    # which stub measure() with an empty report.
    if report.get("run_id"):
        _prepare_evidence_directory(report["run_id"])
    schema_timeout = 90
    try:
        schema = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.json"),
                                 "run", "--rm", "--no-deps", "relay", "python", "-m",
                                 "event_pipeline.schema_registry", "apply"],
                                capture_output=True, text=True, timeout=schema_timeout)
    except subprocess.TimeoutExpired:
        path = evidence / "schema-registration-timeout.json"
        path.write_text(json.dumps({"phase": "schema_registration",
                                    "timed_out": True,
                                    "timeout_seconds": schema_timeout}) + "\n")
        path.chmod(0o600)
        raise ValueError("Dev Schema Registry registration timed out") from None
    path = evidence / "schema-registration.log"
    path.write_text(schema.stdout + "\n" + schema.stderr)
    path.chmod(0o600)
    if schema.returncode:
        raise ValueError("register the fixed Dev schema before starting workers")
    try:
        result = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.json"),
                                 "up", "-d"], capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        path = evidence / "startup-timeout.json"
        path.write_text(json.dumps({"startup_timeout": True, "acceptance": "pending"}))
        path.chmod(0o600)
        raise ValueError("startup deadline reached; preserve resources and remeasure") from None
    path = evidence / "startup.log"
    path.write_text(result.stdout + "\n" + result.stderr)
    path.chmod(0o600)
    if result.returncode:
        raise ValueError("startup failed; preserve private diagnostics")
    summary_topics = _ensure_summary_topics("development")
    topics_path = evidence / "online-summary-topics.json"
    topics_path.write_text(json.dumps({"environment": "development", "topics": summary_topics}, indent=2) + "\n")
    topics_path.chmod(0o600)
    _ensure_task_topics()
    migration = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.json"),
                                "run", "--rm", "--no-deps", "relay", "python", "-m",
                                "event_pipeline.delivery_schema_migrate"],
                               capture_output=True, text=True, timeout=90)
    migration_path = evidence / "delivery-index-migration.log"
    migration_path.write_text(migration.stdout + "\n" + migration.stderr)
    migration_path.chmod(0o600)
    if migration.returncode:
        raise ValueError("Dev delivery index migration failed")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    flink = _submit_and_verify_flink(manifest, evidence)
    return {**report, "startup_requested": True, "flink": flink, "acceptance": "pending"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("measure", "apply"))
    args = parser.parse_args()
    try:
        print(json.dumps(apply() if args.mode == "apply" else measure()))
    except Exception as error:
        print(json.dumps({
            "error": "dev_control_failed",
            "reason": safe_control_failure_detail(error),
        }), file=sys.stderr)
        raise SystemExit("Dev pipeline control refused; inspect private evidence") from None
