"""Measured Staging event-pipeline prepare/apply/rollback controller.

This module is invoked only by the fixed Staging gateway.  It has no paths,
projects, networks, volumes, topics, or database names for Production, Dev, or
Shadow and never performs ``down -v``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any

from . import flink_submission
from .staging_compose import (
    BROKERS, DLQ_TOPIC, EVENT_TOPIC, REGISTRY_TOPIC, compose, model_sha256,
    network_for, project_for,
)
from .staging_prepare import BASE, root_for, snapshot_database
from .staging_metrics_probe import sample as metrics_sample


TOPICS = (EVENT_TOPIC, DLQ_TOPIC, REGISTRY_TOPIC)
FORBIDDEN_TOKENS = ("production", "shadow", "development", "dev_")


def _run(command: list[str], *, timeout: int = 120, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError("Staging event-pipeline command failed")
    return result


def _docker_json(command: list[str]) -> Any:
    return json.loads(_run(command).stdout or "null")


def _json_array_or_lines(value: str) -> list[dict[str, Any]]:
    text = value.strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(decoded, dict):
        return [decoded]
    if not isinstance(decoded, list) or any(not isinstance(item, dict) for item in decoded):
        raise ValueError("Docker JSON output contract changed")
    return decoded


def _load(run_id: str) -> tuple[Path, dict, dict]:
    root = root_for(run_id)
    if root.is_symlink() or root.parent != BASE or not root.is_dir():
        raise ValueError("fixed Staging run root required")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Staging manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("environment") != "staging" or manifest.get("run_id") != run_id
            or manifest.get("project") != project_for(run_id)):
        raise ValueError("Staging manifest identity mismatch")
    for name, digest in manifest.get("hashes", {}).items():
        path = root / name
        if path.is_symlink() or not path.is_file() or root not in path.parents:
            raise ValueError("Staging runtime file identity mismatch")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Staging runtime file identity mismatch")
    spec = json.loads((root / "compose.json").read_text(encoding="utf-8"))
    expected = compose(manifest["images"], run_id)
    if spec != expected or manifest.get("compose_model_sha256") != model_sha256(manifest["images"], run_id):
        raise ValueError("Staging Compose differs from controlled model")
    return root, manifest, spec


def _validate_no_cross_environment(spec: dict) -> None:
    text = json.dumps(spec, sort_keys=True).lower()
    # ``production_data=false`` is an allowed safety label; no other
    # Production identity may appear in the runtime definition.
    text = text.replace('"binhu.production_data": "false"', "")
    for token in FORBIDDEN_TOKENS:
        if token in text:
            raise ValueError("Staging runtime references a forbidden environment")


def _backend_network() -> dict:
    network = _docker_json(["docker", "network", "inspect", "binhu-staging_internal"])[0]
    labels = network.get("Labels", {}) or {}
    if labels.get("com.docker.compose.project") != "binhu-staging" or not network.get("Internal"):
        raise ValueError("isolated Staging Backend network required")
    for item in (network.get("Containers") or {}).values():
        if not str(item.get("Name", "")).startswith("binhu-staging-"):
            raise ValueError("foreign member on Staging Backend network")
    return network


def _active_staging_snapshot(manifest: dict[str, Any]) -> str:
    snapshot_id = str(manifest.get("staging_snapshot_id") or "")
    expected = snapshot_database(snapshot_id, "OnlineData")
    path = Path("/srv/binhu-environments/staging/backend.env")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Staging application environment missing")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if re.fullmatch(r"[A-Z0-9_]+", key):
                values[key] = value
    if (values.get("APP_ENVIRONMENT") != "staging"
            or values.get("MYSQL_HOST") != "environment-mysql"
            or values.get("MYSQL_ONLINE_DATA_DB") != expected):
        raise ValueError("Staging application snapshot identity mismatch")
    return snapshot_id


def measure(run_id: str) -> dict[str, Any]:
    root, manifest, spec = _load(run_id)
    _validate_no_cross_environment(spec)
    backend = _backend_network()
    snapshot_id = _active_staging_snapshot(manifest)
    for image in manifest["images"].values():
        if _run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).stdout.strip() != image:
            raise ValueError("Staging image identity mismatch")
    _run(["docker", "compose", "-f", str(root / "compose.json"), "config", "--quiet"])
    existing = _run([
        "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={manifest['project']}"
    ]).stdout.split()
    if existing:
        for item in _docker_json(["docker", "inspect", *existing]):
            labels = item.get("Config", {}).get("Labels", {}) or {}
            if labels.get("binhu.environment") != "staging" or labels.get("binhu.run_id") != run_id:
                raise ValueError("existing Staging container identity mismatch")
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(memory["MemAvailable"].split()[0])
    if available < 3 * 1024**2:
        raise ValueError("insufficient memory reserve for Staging pipeline")
    stats = os.statvfs(BASE)
    if stats.f_bavail * stats.f_frsize < 10 * 1024**3:
        raise ValueError("insufficient disk reserve for Staging pipeline")
    return {
        "environment": "staging", "run_id": run_id, "project": manifest["project"],
        "hashes_verified": True, "compose_verified": True,
        "backend_network_id": backend.get("Id", "")[:12],
        "staging_snapshot_id": snapshot_id,
        "memory_available_kib": available, "production_access": False,
    }


def _compose(root: Path, *args: str, timeout: int = 420) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "compose", "-f", str(root / "compose.json"), *args], timeout=timeout)


def _container(project: str, service: str) -> str:
    return f"{project}-{service}-1"


def _create_topics(project: str) -> dict[str, Any]:
    broker = _container(project, BROKERS[0])
    for topic in TOPICS:
        _run([
            "docker", "exec", broker, "/opt/kafka/bin/kafka-topics.sh",
            "--bootstrap-server", f"{BROKERS[0]}:9092", "--create", "--if-not-exists",
            "--topic", topic, "--partitions", "3", "--replication-factor", "3",
            "--config", "min.insync.replicas=2",
        ])
    output = _run([
        "docker", "exec", broker, "/opt/kafka/bin/kafka-topics.sh",
        "--bootstrap-server", f"{BROKERS[0]}:9092", "--describe",
    ]).stdout
    if any(topic not in output for topic in TOPICS):
        raise ValueError("Staging Kafka topic verification failed")
    return {"topics": list(TOPICS), "replication_factor": 3, "min_isr": 2}


def _consumer_group(project: str, expected: str) -> list[str]:
    broker = _container(project, BROKERS[0])
    output = _run([
        "docker", "exec", broker, "/opt/kafka/bin/kafka-consumer-groups.sh",
        "--bootstrap-server", f"{BROKERS[0]}:9092", "--describe", "--group", expected,
    ]).stdout
    if EVENT_TOPIC not in output:
        raise ValueError("Staging Flink group has no Staging topic assignment")
    return [expected]


def _flink(root: Path, manifest: dict, evidence: Path) -> dict[str, Any]:
    run_id = manifest["run_id"]
    sql = (root / "pipeline.sql").read_text(encoding="utf-8")
    identity = flink_submission.validate_sql_identity(sql, run_id)
    runtime = (root / "runtime.env").read_text(encoding="utf-8")
    if flink_submission.parse_runtime_identity(runtime) != run_id:
        raise ValueError("Staging runtime and manifest run_id differ")
    project = manifest["project"]
    jobmanager = _container(project, "jobmanager")
    client = flink_submission.FlinkRest(jobmanager)
    overview = flink_submission.wait_for_rest(client)
    current, stale = flink_submission.partition_active_jobs(overview, run_id)
    if stale:
        # A per-run cluster must not contain another run at all.  Refuse rather
        # than cancelling an identity the current command did not create.
        (evidence / "unexpected-flink-jobs.json").write_text(json.dumps({
            "environment": "staging", "run_id": run_id,
            "jobs": [{"jid": x.get("jid"), "name": x.get("name"), "state": x.get("state")} for x in stale],
        }, indent=2) + "\n", encoding="utf-8")
        raise ValueError("foreign Staging Flink job found")
    if len(current) > 1:
        raise ValueError("Staging Flink current run has multiple jobs")
    if not current:
        jar_id = client.upload_jar("/opt/flink/private/pipeline-job.jar")
        client.run_jar(jar_id)
    verified = flink_submission.wait_for_runtime(
        client, run_id, lambda: _consumer_group(project, identity["consumer_group"]), timeout=180,
    )
    payload = {**verified, "identity": identity}
    (evidence / "flink-runtime.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def apply(run_id: str) -> dict[str, Any]:
    report = measure(run_id)
    root, manifest, _ = _load(run_id)
    evidence_root = root / "evidence"
    evidence_root.mkdir(mode=0o700, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="apply-", dir=evidence_root))
    started = time.monotonic()
    try:
        _compose(root, "up", "-d", *BROKERS)
        topics = _create_topics(manifest["project"])
        _compose(root, "up", "-d", "schema-registry", "staging-derived-mysql", "staging-derived-redis")
        schema = None
        for attempt in range(6):
            try:
                schema = _compose(root, "run", "--rm", "--no-deps", "relay", "python", "-m",
                                  "event_pipeline.schema_registry", "apply", timeout=120)
                break
            except ValueError:
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 15))
        if schema is None:
            raise ValueError("Staging Schema Registry did not become ready")
        (evidence / "schema.log").write_text(schema.stdout, encoding="utf-8")
        _compose(root, "run", "--rm", "--no-deps", "relay", "python", "-m",
                 "event_pipeline.delivery_schema_migrate", timeout=120)
        _compose(root, "up", "-d", "relay", "bridge", "business-bridge", "backend-outbox-relay",
                 "python-metadata-worker", "jobmanager", "taskmanager")
        flink = _flink(root, manifest, evidence)
        manifest.update({"started": True, "acceptance": "pending"})
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {**report, "topics": topics, "flink": flink, "acceptance": "pending",
                "elapsed_seconds": round(time.monotonic() - started, 3)}
    except BaseException:
        (evidence / "failure.json").write_text(json.dumps({
            "environment": "staging", "run_id": run_id, "acceptance": "failed",
        }) + "\n", encoding="utf-8")
        raise


def rollback(run_id: str) -> dict[str, Any]:
    root, manifest, _ = _load(run_id)
    started = time.monotonic()
    project = manifest["project"]
    client = flink_submission.FlinkRest(_container(project, "jobmanager"))
    current, foreign = flink_submission.partition_active_jobs(client.overview(), run_id)
    if foreign:
        raise ValueError("foreign Staging Flink job found")
    for item in current:
        client.cancel(str(item.get("jid", "")))
    _compose(root, "up", "-d", "python-metadata-worker")
    probe = _compose(root, "run", "--rm", "--no-deps", "python-metadata-worker",
                     "python", "-m", "event_pipeline.staging_rollback_verify", timeout=120)
    result = json.loads(probe.stdout.strip().splitlines()[-1])
    if result.get("consistent") is not True:
        raise ValueError("Staging rollback data consistency failed")
    states = _json_array_or_lines(_compose(root, "ps", "--format", "json").stdout)
    worker = next((item for item in states if item.get("Service") == "python-metadata-worker"), {})
    healthy = str(worker.get("State", "")).lower() == "running"
    if not healthy:
        raise ValueError("Staging Python worker did not recover")
    return {
        "environment": "staging", "run_id": run_id, "production_data": False,
        "status": "passed", "python_worker_restored": True,
        "data_consistency_verified": True, "legacy_path_healthy": True,
        "projection_rows": result.get("python_projection_rows", 0),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("measure", "apply", "sample", "rollback"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        action = {"measure": measure, "apply": apply, "sample": metrics_sample,
                  "rollback": rollback}[args.action]
        print(json.dumps(action(args.run_id), sort_keys=True))
    except Exception:
        raise SystemExit("Staging event-pipeline control refused; inspect private evidence") from None


if __name__ == "__main__":
    main()
