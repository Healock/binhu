#!/usr/bin/env python3
"""Root-side fixed gateway for the isolated Staging event pipeline."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone


STATE = Path("/var/lib/binhu-staging-event-pipeline")
CANDIDATES = STATE / "candidates"
RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BYTES = 128 * 1024 * 1024
MEMBERS = frozenset({"source.tar.gz", "pipeline-job.jar", "manifest.json", "SHA256SUMS"})
RETIRE_TARGET = "STG-20261007-03"
RETIRE_SUCCESSOR = "STG-20261027-02"
FAILED_RETIRE_TARGET = "STG-20261027-04"
FAILED_RETIRE_SUCCESSOR = "STG-20261027-02"
BACKEND_NETWORK = "binhu-staging-pipeline-backend"
STAGING_CORE = (
    "binhu-staging-backend-1", "binhu-staging-environment-mysql-1", "binhu-staging-redis-1",
)
PRODUCTION_CORE = ("binhu-backend", "binhu-mysql", "binhu-ops-agent")
EVIDENCE_MEMBERS = frozenset({
    "load-report.json", "resource-samples.json", "rollback-report.json", "manual-report.json",
})


def fail(message: str) -> None:
    raise SystemExit("Staging event-pipeline gateway refused: " + message)


def _child_error_code(stdout: str, stderr: str) -> str:
    """Classify child failure text without exposing command output."""
    text = f"{stdout}\n{stderr}".lower()
    markers = (
        ("modulenotfounderror", "staging_module_import_failed"),
        ("importerror", "staging_module_import_failed"),
        ("syntaxerror", "staging_source_syntax_invalid"),
        ("filenotfounderror", "staging_required_file_missing"),
        ("permissionerror", "staging_permission_denied"),
        ("no such image", "staging_image_missing"),
        ("manifest unknown", "staging_image_missing"),
        ("credential", "staging_credentials_missing"),
        ("password", "staging_credentials_invalid"),
        ("no space left", "staging_insufficient_disk"),
        ("address pool", "staging_network_address_pool_exhausted"),
        ("cannot connect to the docker daemon", "staging_docker_unavailable"),
        ("error response from daemon", "staging_docker_daemon_error"),
        ("snapshot", "staging_snapshot_invalid"),
        ("redis", "staging_redis_configuration_invalid"),
        ("mysql", "staging_mysql_configuration_invalid"),
        ("backend", "staging_backend_configuration_invalid"),
        ("isolated", "staging_isolation_configuration_invalid"),
        ("identity", "staging_identity_invalid"),
        ("required", "staging_required_configuration_missing"),
        ("configuration", "staging_configuration_invalid"),
    )
    for marker, code in markers:
        if marker in text:
            return code
    return "staging_child_process_failed"


def _child_payloads(stdout: str, stderr: str) -> list[dict]:
    """Extract bounded JSON diagnostics without forwarding child output."""
    payloads: list[dict] = []
    for line in (stdout + "\n" + stderr).splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            start, end = candidate.find("{"), candidate.rfind("}")
            if start < 0 or end <= start:
                continue
            candidate = candidate[start:end + 1]
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def _record_child_failure(run_id: str, phase: str, payload: dict,
                          stdout: str, stderr: str) -> None:
    """Persist only redacted failure metadata and output hashes."""
    try:
        digest = hashlib.sha256((stdout + "\0" + stderr).encode("utf-8", "replace")).hexdigest()
        evidence = STATE / "evidence" / run_id
        evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = evidence / f"{phase}-{digest[:16]}.json"
        if target.exists() or target.is_symlink():
            return
        safe = {
            "environment": "staging", "run_id": run_id, "phase": phase,
            "status": "failed", "error_type": str(payload.get("error_type", "CalledProcessError")),
            "error_code": str(payload.get("error_code", "staging_child_process_failed")),
            "stdout_sha256": hashlib.sha256(stdout.encode("utf-8", "replace")).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr.encode("utf-8", "replace")).hexdigest(),
            "stdout_bytes": len(stdout.encode("utf-8", "replace")),
            "stderr_bytes": len(stderr.encode("utf-8", "replace")),
        }
        if isinstance(payload.get("image_key"), str) and re.fullmatch(r"[a-z_]{1,40}", payload["image_key"]):
            safe["image_key"] = payload["image_key"]
        target.write_text(json.dumps(safe, sort_keys=True) + "\n", encoding="utf-8")
        target.chmod(0o600)
    except (OSError, ValueError):
        # Failure evidence must never hide the original deployment failure.
        return


def read_bundle(size: int, expected: str, destination: Path) -> None:
    if size <= 0 or size > MAX_BYTES or not SHA_RE.fullmatch(expected):
        fail("candidate size or digest invalid")
    digest = hashlib.sha256()
    remaining = size
    with destination.open("xb") as stream:
        while remaining:
            block = sys.stdin.buffer.read(min(1024 * 1024, remaining))
            if not block:
                fail("candidate upload truncated")
            stream.write(block)
            digest.update(block)
            remaining -= len(block)
    if digest.hexdigest() != expected:
        fail("candidate bundle hash mismatch")


def _safe(member: tarfile.TarInfo) -> bool:
    return member.isfile() and not member.issym() and not member.islnk() and member.name in MEMBERS


def extract_candidate(bundle: Path, target: Path, run_id: str, commit: str) -> dict:
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        if {item.name for item in members} != MEMBERS or any(not _safe(item) for item in members):
            fail("candidate contents are not fixed")
        payload = {item.name: archive.extractfile(item).read() for item in members}
    try:
        manifest = json.loads(payload["manifest.json"])
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("candidate manifest invalid")
    if (manifest.get("environment") != "staging" or manifest.get("run_id") != run_id
            or manifest.get("commit") != commit or manifest.get("ready_for_staging") is not True
            or manifest.get("production_data") is not False):
        fail("candidate identity mismatch")
    expected = {name: hashlib.sha256(payload[name]).hexdigest()
                for name in ("source.tar.gz", "pipeline-job.jar", "manifest.json")}
    actual = {line.split("  ", 1)[1]: line.split("  ", 1)[0]
              for line in payload["SHA256SUMS"].decode("ascii").splitlines() if "  " in line}
    if actual != expected:
        fail("candidate checksums invalid")
    source = target / "source"
    source.mkdir(mode=0o700)
    source_archive = target / "source.tar.gz"
    source_archive.write_bytes(payload["source.tar.gz"])
    with tarfile.open(source_archive, "r:gz") as archive:
        for item in archive.getmembers():
            path = Path(item.name)
            if (not item.isfile() or item.issym() or item.islnk() or path.is_absolute()
                    or ".." in path.parts or not (
                        item.name.startswith("deploy/environments/event_pipeline/")
                        or item.name.startswith("load-tests/staging/")
                        or item.name == "load-tests/requirements.txt")):
                fail("candidate source entry invalid")
        archive.extractall(source)
    jar = target / "pipeline-job.jar"
    jar.write_bytes(payload["pipeline-job.jar"])
    (target / "manifest.json").write_bytes(payload["manifest.json"])
    for path in (source_archive, jar, target / "manifest.json"):
        path.chmod(0o600)
    return manifest


def environment() -> dict[str, str]:
    result = os.environ.copy()
    config = Path("/etc/binhu-staging-event-pipeline.conf")
    if not config.is_file() or config.is_symlink():
        fail("private Staging configuration missing")
    for line in config.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if re.fullmatch(r"[A-Z0-9_]+", key):
                result[key] = value
    return result


def run_module(run_id: str, module: str, manifest: dict | None = None,
               production_since_unix: int | None = None) -> dict:
    candidate = CANDIDATES / run_id
    source = candidate / "source"
    env = environment()
    env.update({"APP_ENVIRONMENT": "staging", "STAGING_RUN_ID": run_id,
                "PIPELINE_RUN_ID": run_id,
                "PYTHONPATH": str(source / "deploy" / "environments")})
    if module == "prepare":
        if manifest is None:
            fail("candidate manifest missing")
        args = [sys.executable, "-m", "event_pipeline.staging_prepare", "--run-id", run_id,
                "--source", str(source / "deploy" / "environments" / "event_pipeline"),
                "--pipeline-jar", str(candidate / "pipeline-job.jar"),
                "--staging-snapshot-id", str(manifest["staging_snapshot_id"])]
        for name in sorted(manifest["images"]):
            args += [f"--{name.replace('_', '-')}-image", manifest["images"][name]]
    else:
        args = [sys.executable, "-m", "event_pipeline.staging_control", module, "--run-id", run_id]
        if module == "sample":
            if production_since_unix is None:
                fail("Production monitor start time required")
            args += ["--production-since", str(production_since_unix)]
    try:
        result = subprocess.run(
            args, cwd=source, env=env, check=True, capture_output=True, text=True,
            timeout=900 if module == "apply" else 300,
        )
    except subprocess.CalledProcessError as error:
        if module in {"prepare", "apply"}:
            # The child emits bounded JSON on stdout, while import/runtime
            # failures can only reach stderr. Inspect both streams without
            # forwarding arbitrary command output or credentials.
            child_stdout = error.stdout or ""
            child_stderr = error.stderr or ""
            emitted = False
            for payload in _child_payloads(child_stdout, child_stderr):
                if (isinstance(payload, dict)
                        and payload.get("environment") == "staging"
                        and payload.get("run_id") == run_id
                        and payload.get("status") == "failed"
                        and payload.get("phase") in {
                            "prepare",
                            "compose_brokers", "create_topics", "start_schema_dependencies",
                            "apply_schema_registry", "migrate_delivery_schema",
                            "start_pipeline_services", "start_flink_job",
                        }
                        and isinstance(payload.get("error_code"), str)
                        and re.fullmatch(r"[A-Za-z0-9_. -]{1,96}", payload["error_code"] or "")):
                    bounded = {
                        "environment": "staging", "run_id": run_id,
                        "status": "failed", "phase": payload["phase"],
                        "error_type": str(payload.get("error_type", "ChildError")),
                        "error_code": payload["error_code"],
                    }
                    if isinstance(payload.get("image_key"), str) and re.fullmatch(r"[a-z_]{1,40}", payload["image_key"]):
                        bounded["image_key"] = payload["image_key"]
                    _record_child_failure(run_id, module, bounded, child_stdout, child_stderr)
                    print(json.dumps(bounded, sort_keys=True))
                    emitted = True
                    break
            if not emitted:
                bounded = {
                    "environment": "staging", "run_id": run_id,
                    "status": "failed", "phase": module,
                    "error_type": type(error).__name__,
                    "error_code": _child_error_code(child_stdout, child_stderr),
                }
                _record_child_failure(run_id, module, bounded, child_stdout, child_stderr)
                print(json.dumps(bounded, sort_keys=True))
        raise
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        fail("fixed control result missing")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        fail("fixed control result invalid")
    if payload.get("environment") != "staging" or payload.get("run_id") != run_id:
        fail("fixed control result identity mismatch")
    return payload


def status() -> None:
    current = STATE / "current.json"
    if current.is_file() and not current.is_symlink():
        print(current.read_text(encoding="utf-8"))
    else:
        print(json.dumps({"environment": "staging", "status": "empty"}))


def prepare(run_id: str, commit: str, size_text: str, digest: str) -> None:
    if not RUN_RE.fullmatch(run_id) or not COMMIT_RE.fullmatch(commit):
        fail("prepare identity invalid")
    target = CANDIDATES / run_id
    if target.exists() or target.is_symlink():
        fail("run id already exists")
    target.mkdir(mode=0o700, parents=True)
    try:
        bundle = target / "candidate.tar.gz"
        read_bundle(int(size_text), digest, bundle)
        manifest = extract_candidate(bundle, target, run_id, commit)
        run_module(run_id, "prepare", manifest)
        print(json.dumps({"environment": "staging", "run_id": run_id, "prepared": True}))
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def control(run_id: str, action: str, production_since_unix: int | None = None) -> None:
    if not RUN_RE.fullmatch(run_id) or not (CANDIDATES / run_id).is_dir():
        fail("prepared Staging candidate required")
    result = run_module(run_id, action, production_since_unix=production_since_unix)
    manifest = json.loads(((CANDIDATES / run_id) / "manifest.json").read_text(encoding="utf-8"))
    if action in {"apply", "switch"}:
        if action == "apply":
            manifest.update({"started": True, "acceptance": "pending"})
        else:
            manifest.update({"acceptance": "pending"})
        current = STATE / "current.json"
        current.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        current.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _staging_backend(run_id: str) -> tuple[str, Path]:
    environment_root = Path("/srv/binhu-environments/staging")
    manifest_path = environment_root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail("Staging application manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate = json.loads(((CANDIDATES / run_id) / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("environment") != "staging" or manifest.get("project") != "binhu-staging"
            or manifest.get("images", {}).get("backend") != candidate.get("candidate_image_digest")):
        fail("Staging application is not the Dev-accepted immutable candidate")
    ids = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=^/binhu-staging-backend-1$"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.split()
    if len(ids) != 1:
        fail("isolated Staging backend container missing")
    inspected = json.loads(subprocess.run(
        ["docker", "inspect", ids[0]], check=True, capture_output=True, text=True, timeout=30,
    ).stdout)[0]
    labels = inspected.get("Config", {}).get("Labels", {}) or {}
    if labels.get("binhu.environment") != "staging" or inspected.get("Image") != candidate["candidate_image_digest"]:
        fail("Staging backend identity mismatch")
    return candidate["candidate_image_digest"], environment_root


def seed(run_id: str) -> None:
    if not RUN_RE.fullmatch(run_id) or not (CANDIDATES / run_id).is_dir():
        fail("prepared Staging candidate required")
    evidence = STATE / "evidence" / run_id
    index = evidence / "runtime-index.json"
    if evidence.is_symlink() or index.exists() or index.is_symlink():
        fail("Staging fixture run already exists")
    image, environment_root = _staging_backend(run_id)
    env = environment()
    password = env.get("STAGING_LOAD_TEST_PASSWORD", "")
    if len(password) < 16:
        fail("private Staging load-test password missing")
    source = (CANDIDATES / run_id) / "source" / "load-tests" / "staging"
    if source.is_symlink() or not (source / "seed.py").is_file():
        fail("fixed Staging seeder missing")
    command = [
        "docker", "run", "--rm", "-i", "--network", "binhu-staging_internal",
        "--label", "binhu.environment=staging", "--label", f"binhu.run_id={run_id}",
        "--memory", "768m", "--cpus", "1", "--pids-limit", "128", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--env-file", str(environment_root / "backend.env"),
        "--env", f"STAGING_LOAD_TEST_RUN_ID={run_id}",
        "--volume", f"{source}:/staging-load:ro",
        "--entrypoint", "python", image, "/staging-load/seed.py",
        "--run-id", run_id, "--password-stdin",
    ]
    result = subprocess.run(
        command, input=password + "\n", capture_output=True, text=True, timeout=900,
    )
    password = ""
    if result.returncode:
        fail("Staging fixture seed failed; inspect private server evidence")
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    if len(lines) != 1:
        fail("Staging fixture runtime index missing")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError:
        fail("Staging fixture runtime index invalid")
    if (payload.get("run_id") != run_id or payload.get("environment") != "staging"
            or payload.get("production_data") is not False or payload.get("fictional_only") is not True
            or len(payload.get("accounts") or []) < 75):
        fail("Staging fixture runtime identity mismatch")
    evidence.mkdir(parents=True, mode=0o700, exist_ok=True)
    index.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    index.chmod(0o600)
    # stdout is designed to be redirected directly to a private runner file.
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def verify(run_id: str, size_text: str, digest: str) -> None:
    if not RUN_RE.fullmatch(run_id) or not (CANDIDATES / run_id).is_dir():
        fail("prepared Staging candidate required")
    size = int(size_text)
    if size <= 0 or size > 32 * 1024 * 1024 or not SHA_RE.fullmatch(digest):
        fail("evidence bundle identity invalid")
    evidence = STATE / "evidence" / run_id
    evidence.mkdir(parents=True, mode=0o700, exist_ok=True)
    bundle = evidence / "acceptance-evidence.tar.gz"
    if bundle.exists() or bundle.is_symlink():
        fail("acceptance evidence already exists")
    read_bundle(size, digest, bundle)
    target = evidence / "acceptance"
    target.mkdir(mode=0o700)
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        if ({item.name for item in members} != EVIDENCE_MEMBERS
                or any(not item.isfile() or item.issym() or item.islnk() for item in members)):
            fail("acceptance evidence contents are not fixed")
        archive.extractall(target)
    source = (CANDIDATES / run_id) / "source"
    env = environment()
    env.update({"APP_ENVIRONMENT": "staging", "STAGING_LOAD_TEST_RUN_ID": run_id,
                "PYTHONPATH": str(source / "load-tests")})
    args = [sys.executable, "-m", "staging.control", "verify", "--run-id", run_id,
            "--manifest", str(CANDIDATES / run_id / "manifest.json"),
            "--load-report", str(target / "load-report.json"),
            "--resource-samples", str(target / "resource-samples.json"),
            "--rollback-report", str(target / "rollback-report.json"),
            "--manual-report", str(target / "manual-report.json")]
    result = subprocess.run(args, cwd=source, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode:
        fail("Staging acceptance verification failed")
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    if len(lines) != 1:
        fail("Staging acceptance result missing")
    report = json.loads(lines[0])
    final = evidence / "final-report.json"
    final.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    final.chmod(0o600)
    print(json.dumps({"environment": "staging", "run_id": run_id,
                      "acceptance": report.get("acceptance", "failed")}))


def _retirement_command(root: Path) -> list[str]:
    return ["docker", "compose", "-f", str(root / "compose.json"), "down", "--remove-orphans"]


def _retirement_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError("staging_retirement_compose_failed")
    return result


def _retirement_json(command: list[str]) -> object:
    result = _retirement_run(command)
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        raise ValueError("staging_retirement_docker_contract_invalid") from None


def _retirement_file(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("staging_retirement_identity_file_missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("staging_retirement_identity_file_invalid") from None
    if not isinstance(value, dict):
        raise ValueError("staging_retirement_identity_file_invalid")
    return value


def _retirement_identity(manifest: dict, current: dict, spec: dict, run_id: str) -> tuple[Path, str]:
    project = "binhu-staging-event-pipeline-" + run_id.lower()
    root = Path("/srv/binhu-environments/staging-event-pipeline") / run_id
    if (run_id != RETIRE_TARGET or manifest.get("environment") != "staging"
            or manifest.get("run_id") != run_id or manifest.get("project") != project
            or manifest.get("production_data") not in (None, False)
            or current.get("environment") != "staging" or current.get("run_id") != run_id
            or current.get("project") != project or current.get("started") is not True
            or current.get("acceptance") == "passed"):
        raise ValueError("staging_retirement_run_identity_mismatch")
    architecture = current.get("architecture", manifest.get("architecture"))
    if isinstance(architecture, dict) and architecture.get("active") is True:
        raise ValueError("staging_retirement_architecture_active")
    if (root.is_symlink() or not root.is_dir() or spec.get("name") != project
            or set(spec.get("services", {})) != set(manifest.get("images", {}).get("services", []))):
        # Older manifests do not keep a service list; their compose model is
        # still bounded by the fixed run-scoped project name and labels below.
        if (root.is_symlink() or not root.is_dir() or spec.get("name") != project
                or not isinstance(spec.get("services"), dict) or not spec.get("services")):
            raise ValueError("staging_retirement_compose_identity_mismatch")
    backend = spec.get("networks", {}).get("backend", {})
    if backend != {"external": True, "name": BACKEND_NETWORK}:
        raise ValueError("staging_retirement_network_contract_mismatch")
    forbidden = json.dumps(spec, sort_keys=True).lower()
    production_identity = "binhu-" + "production"
    if production_identity in forbidden or "production_data\": true" in forbidden:
        raise ValueError("staging_retirement_production_reference")
    compose_path = root / "compose.json"
    expected_hash = (manifest.get("hashes") or {}).get("compose.json")
    if (compose_path.is_symlink() or not compose_path.is_file() or not isinstance(expected_hash, str)
            or hashlib.sha256(compose_path.read_bytes()).hexdigest() != expected_hash):
        raise ValueError("staging_retirement_compose_hash_mismatch")
    return root, project


def _failed_retirement_identity(manifest: dict, spec: dict, run_id: str) -> tuple[Path, str]:
    project = "binhu-staging-event-pipeline-" + run_id.lower()
    root = Path("/srv/binhu-environments/staging-event-pipeline") / run_id
    if (run_id != FAILED_RETIRE_TARGET or manifest.get("environment") != "staging"
            or manifest.get("run_id") != run_id or manifest.get("project") != project
            or manifest.get("production_data") not in (None, False)):
        raise ValueError("staging_retirement_run_identity_mismatch")
    if (root.is_symlink() or not root.is_dir() or spec.get("name") != project
            or not isinstance(spec.get("services"), dict) or not spec.get("services")):
        raise ValueError("staging_retirement_compose_identity_mismatch")
    backend = spec.get("networks", {}).get("backend", {})
    if backend != {"external": True, "name": BACKEND_NETWORK}:
        raise ValueError("staging_retirement_network_contract_mismatch")
    forbidden = json.dumps(spec, sort_keys=True).lower()
    production_identity = "binhu-" + "production"
    if production_identity in forbidden or 'production_data": true' in forbidden:
        raise ValueError("staging_retirement_production_reference")
    compose_path = root / "compose.json"
    expected_hash = (manifest.get("hashes") or {}).get("compose.json")
    if (compose_path.is_symlink() or not compose_path.is_file() or not isinstance(expected_hash, str)
            or hashlib.sha256(compose_path.read_bytes()).hexdigest() != expected_hash):
        raise ValueError("staging_retirement_compose_hash_mismatch")
    return root, project


def _retirement_inspect(names: list[str]) -> dict[str, dict]:
    if not names:
        return {}
    payload = _retirement_json(["docker", "inspect", *names])
    if not isinstance(payload, list) or len(payload) != len(names):
        raise ValueError("staging_retirement_container_identity_invalid")
    result = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("staging_retirement_container_identity_invalid")
        name = str(item.get("Name", "")).lstrip("/")
        state = item.get("State") or {}
        labels = (item.get("Config") or {}).get("Labels") or {}
        networks = item.get("NetworkSettings", {}).get("Networks", {}) or {}
        result[name] = {
            "id": str(item.get("Id", "")), "image": str(item.get("Image", "")),
            "status": state.get("Status"), "started_at": state.get("StartedAt"),
            "restart_count": item.get("RestartCount"),
            "environment": labels.get("binhu.environment"), "run_id": labels.get("binhu.run_id"),
            "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
            "networks": sorted(networks),
        }
    return result


def _retirement_jobs(jobmanager: str, run_id: str) -> list[dict]:
    result = subprocess.run(
        ["docker", "exec", jobmanager, "/bin/sh", "-c",
         "wget -qO- http://127.0.0.1:8081/jobs/overview"],
        check=False, capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise ValueError("staging_retirement_flink_probe_failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError("staging_retirement_flink_probe_invalid") from None
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("staging_retirement_flink_probe_invalid")
    safe = []
    terminal_states = {"CANCELED", "FAILED", "FINISHED", "SUSPENDED"}
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("staging_retirement_foreign_flink_job")
        state = str(job.get("state", "")).upper()
        if job.get("name") != run_id and state not in terminal_states:
            raise ValueError("staging_retirement_foreign_flink_job")
        safe.append({
            "jid": job.get("jid"),
            "name": job.get("name") if job.get("name") == run_id else "terminal-job",
            "state": state,
        })
    return safe


def _retirement_snapshot(run_id: str, project: str, *, successor: str = RETIRE_SUCCESSOR) -> dict:
    ids = _retirement_run([
        "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
    ]).stdout.split()
    old = _retirement_inspect(ids)
    if not old:
        raise ValueError("staging_retirement_target_containers_missing")
    for name, item in old.items():
        if (item["environment"] != "staging" or item["run_id"] != run_id
                or item["project"] != project or item["status"] != "running"):
            raise ValueError("staging_retirement_target_container_identity_mismatch")

    core = _retirement_inspect([*STAGING_CORE, *PRODUCTION_CORE])
    if set(core) != set((*STAGING_CORE, *PRODUCTION_CORE)) or any(
            core[name]["status"] != "running" for name in (*STAGING_CORE, *PRODUCTION_CORE)):
        raise ValueError("staging_retirement_core_service_unhealthy")

    network_payload = _retirement_json(["docker", "network", "inspect", BACKEND_NETWORK])
    if not isinstance(network_payload, list) or len(network_payload) != 1:
        raise ValueError("staging_retirement_backend_network_invalid")
    network = network_payload[0]
    labels = network.get("Labels") or {}
    members = {entry.get("Name") for entry in (network.get("Containers") or {}).values()}
    expected_members = {
        "binhu-staging-environment-mysql-1", "binhu-staging-redis-1",
        f"binhu-staging-event-pipeline-{run_id.lower()}-business-bridge-1",
        f"binhu-staging-event-pipeline-{run_id.lower()}-backend-outbox-relay-1",
        f"binhu-staging-event-pipeline-{successor.lower()}-business-bridge-1",
        f"binhu-staging-event-pipeline-{successor.lower()}-backend-outbox-relay-1",
    }
    if (network.get("Name") != BACKEND_NETWORK or network.get("Driver") != "bridge"
            or network.get("Internal") is not True or labels.get("binhu.environment") != "staging"
            or labels.get("binhu.role") != "pipeline-backend-access" or members != expected_members):
        raise ValueError("staging_retirement_backend_network_membership_mismatch")
    connectors = _retirement_inspect(sorted(expected_members - {
        "binhu-staging-environment-mysql-1", "binhu-staging-redis-1",
    }))
    for name, item in connectors.items():
        expected_run = successor if successor.lower() in name else run_id
        expected_service = "business-bridge" if name.endswith("business-bridge-1") else "backend-outbox-relay"
        if (item["status"] != "running" or item["environment"] != "staging"
                or item["run_id"] != expected_run or item["project"] != "binhu-staging-event-pipeline-" + expected_run.lower()
                or item["service"] != expected_service or BACKEND_NETWORK not in item["networks"]):
            raise ValueError("staging_retirement_replacement_connector_invalid")

    volumes = _retirement_run([
        "docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}",
    ]).stdout.split()
    volume_details = _retirement_json(["docker", "volume", "inspect", *volumes]) if volumes else []
    if not isinstance(volume_details, list) or len(volume_details) != len(volumes):
        raise ValueError("staging_retirement_volume_identity_invalid")
    safe_volumes = sorted(str(item.get("Name", "")) for item in volume_details if isinstance(item, dict))
    if safe_volumes != sorted(volumes):
        raise ValueError("staging_retirement_volume_identity_invalid")

    jobs = _retirement_jobs(
        f"binhu-staging-event-pipeline-{run_id.lower()}-jobmanager-1", run_id,
    )
    available = None
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            available = int(line.split()[1])
            break
    if available is None:
        raise ValueError("staging_retirement_memory_probe_failed")
    return {
        "target_containers": old, "volumes": safe_volumes,
        "backend_network_id": network.get("Id"), "backend_members": sorted(members),
        "replacement_connectors": connectors, "core_services": core,
        "active_flink_jobs": jobs, "mem_available_kib": available,
    }


def retire(run_id: str) -> None:
    if run_id != RETIRE_TARGET:
        fail("retirement target is not approved")
    candidate = Path("/srv/binhu-environments/staging-event-pipeline") / run_id
    current_path = STATE / "current.json"
    manifest_path = candidate / "manifest.json"
    compose_path = candidate / "compose.json"
    manifest = _retirement_file(manifest_path)
    current = _retirement_file(current_path)
    spec = _retirement_file(compose_path)
    root, project = _retirement_identity(manifest, current, spec, run_id)
    evidence_root = STATE / "evidence" / run_id
    if evidence_root.is_symlink() or evidence_root.parent.is_symlink():
        fail("retirement evidence path invalid")
    evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = evidence_root / f"retirement-{stamp}"
    evidence.mkdir(mode=0o700)
    before = _retirement_snapshot(run_id, project)
    before.update({
        "environment": "staging", "run_id": run_id, "project": project,
        "current_sha256": hashlib.sha256(current_path.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "compose_sha256": hashlib.sha256(compose_path.read_bytes()).hexdigest(),
        "event_projection_paused_until_successor_apply": bool(before["active_flink_jobs"]),
    })
    _write_private_evidence(evidence, "before.json", before)
    try:
        _retirement_run(_retirement_command(root), timeout=300)
        after = _retirement_snapshot_after(run_id, project, before)
        current.update({
            "lifecycle_status": "retired", "retired_at": datetime.now(timezone.utc).isoformat(),
            "retirement_evidence": str(evidence),
        })
        staged = current_path.with_name("current.json.retirement-new")
        if staged.exists() or staged.is_symlink():
            raise ValueError("staging_retirement_state_temp_exists")
        staged.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staged.chmod(0o600)
        os.replace(staged, current_path)
        result = {
            "environment": "staging", "run_id": run_id, "status": "retired",
            "containers_stopped": len(before["target_containers"]),
            "volumes_preserved": before["volumes"], "active_flink_jobs_stopped": before["active_flink_jobs"],
            "replacement_connectors_verified": sorted(before["replacement_connectors"]),
            "backend_network_preserved": before["backend_network_id"] == after["backend_network_id"],
            "production_and_staging_core_unchanged": before["core_services"] == after["core_services"],
            "mem_available_kib": after["mem_available_kib"],
            "production_data": False, "evidence": str(evidence),
        }
        _write_private_evidence(evidence, "result.json", result)
        _write_retirement_hashes(evidence)
        print(json.dumps(result, sort_keys=True))
    except BaseException as error:
        safe_code = str(error) if isinstance(error, ValueError) and re.fullmatch(r"staging_retirement_[a-z_]+", str(error)) else "staging_retirement_failed"
        _write_private_evidence(evidence, "failure.json", {
            "environment": "staging", "run_id": run_id, "status": "failed",
            "error_code": safe_code,
        })
        _write_retirement_hashes(evidence)
        raise


def retire_failed(run_id: str) -> None:
    """Retire one failed, non-current run without changing the active pointer."""
    if run_id != FAILED_RETIRE_TARGET:
        fail("failed retirement target is not approved")
    candidate = Path("/srv/binhu-environments/staging-event-pipeline") / run_id
    manifest_path = candidate / "manifest.json"
    compose_path = candidate / "compose.json"
    manifest = _retirement_file(manifest_path)
    spec = _retirement_file(compose_path)
    root, project = _failed_retirement_identity(manifest, spec, run_id)
    evidence_root = STATE / "evidence" / run_id
    if evidence_root.is_symlink() or evidence_root.parent.is_symlink():
        fail("retirement evidence path invalid")
    evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = evidence_root / f"retirement-{stamp}"
    evidence.mkdir(mode=0o700)
    before = _retirement_snapshot(run_id, project, successor=FAILED_RETIRE_SUCCESSOR)
    if before["active_flink_jobs"]:
        raise ValueError("staging_retirement_active_flink_job")
    before.update({
        "environment": "staging", "run_id": run_id, "project": project,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "compose_sha256": hashlib.sha256(compose_path.read_bytes()).hexdigest(),
        "active_pointer_unchanged": True,
    })
    _write_private_evidence(evidence, "before.json", before)
    try:
        _retirement_run(_retirement_command(root), timeout=300)
        after = _retirement_snapshot_after(
            run_id, project, before, successor=FAILED_RETIRE_SUCCESSOR,
        )
        result = {
            "environment": "staging", "run_id": run_id, "status": "retired",
            "containers_stopped": len(before["target_containers"]),
            "volumes_preserved": before["volumes"],
            "active_flink_jobs_stopped": before["active_flink_jobs"],
            "backend_network_preserved": before["backend_network_id"] == after["backend_network_id"],
            "production_and_staging_core_unchanged": before["core_services"] == after["core_services"],
            "active_pointer_unchanged": True,
            "mem_available_kib": after["mem_available_kib"],
            "production_data": False, "evidence": str(evidence),
        }
        _write_private_evidence(evidence, "result.json", result)
        _write_retirement_hashes(evidence)
        print(json.dumps(result, sort_keys=True))
    except BaseException as error:
        safe_code = str(error) if isinstance(error, ValueError) and re.fullmatch(
            r"staging_retirement_[a-z_]+", str(error)
        ) else "staging_retirement_failed"
        _write_private_evidence(evidence, "failure.json", {
            "environment": "staging", "run_id": run_id, "status": "failed",
            "error_code": safe_code,
        })
        _write_retirement_hashes(evidence)
        raise


def _write_private_evidence(directory: Path, name: str, payload: dict) -> None:
    target = directory / name
    with target.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    target.chmod(0o600)


def _write_retirement_hashes(directory: Path) -> None:
    hashes = {item.name: hashlib.sha256(item.read_bytes()).hexdigest()
              for item in directory.iterdir() if item.is_file() and item.name != "sha256.json"}
    _write_private_evidence(directory, "sha256.json", hashes)


def _retirement_snapshot_after(
    run_id: str, project: str, before: dict, *, successor: str = RETIRE_SUCCESSOR,
) -> dict:
    ids = _retirement_run([
        "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
    ]).stdout.split()
    if ids:
        raise ValueError("staging_retirement_target_containers_remain")
    volumes = _retirement_run([
        "docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}",
    ]).stdout.split()
    if sorted(volumes) != before["volumes"]:
        raise ValueError("staging_retirement_volume_set_changed")
    core = _retirement_inspect([*STAGING_CORE, *PRODUCTION_CORE])
    if core != before["core_services"]:
        raise ValueError("staging_retirement_core_service_changed")
    network_payload = _retirement_json(["docker", "network", "inspect", BACKEND_NETWORK])
    if not isinstance(network_payload, list) or len(network_payload) != 1:
        raise ValueError("staging_retirement_backend_network_changed")
    network = network_payload[0]
    members = {entry.get("Name") for entry in (network.get("Containers") or {}).values()}
    expected_members = set(before["backend_members"]) - {
        f"binhu-staging-event-pipeline-{run_id.lower()}-business-bridge-1",
        f"binhu-staging-event-pipeline-{run_id.lower()}-backend-outbox-relay-1",
    }
    if (network.get("Id") != before["backend_network_id"] or members != expected_members):
        raise ValueError("staging_retirement_backend_network_changed")
    available = None
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            available = int(line.split()[1])
            break
    if available is None:
        raise ValueError("staging_retirement_memory_probe_failed")
    return {
        "backend_network_id": network.get("Id"),
        "backend_members": sorted(members),
        "core_services": core,
        "mem_available_kib": available,
    }


def main() -> None:
    if os.geteuid() != 0:
        fail("root execution required")
    if len(sys.argv) == 2 and sys.argv[1] == "status":
        status(); return
    if len(sys.argv) == 6 and sys.argv[1] == "prepare":
        prepare(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]); return
    if len(sys.argv) == 3 and sys.argv[1] in {"measure", "apply", "switch", "rollback"}:
        control(sys.argv[2], sys.argv[1]); return
    if len(sys.argv) == 3 and sys.argv[1] == "retire":
        retire(sys.argv[2]); return
    if len(sys.argv) == 3 and sys.argv[1] == "retire-failed":
        retire_failed(sys.argv[2]); return
    if (len(sys.argv) == 4 and sys.argv[1] == "sample"
            and re.fullmatch(r"[1-9][0-9]{9}", sys.argv[3])):
        control(sys.argv[2], "sample", int(sys.argv[3])); return
    if len(sys.argv) == 3 and sys.argv[1] == "run":
        seed(sys.argv[2]); return
    if len(sys.argv) == 5 and sys.argv[1] == "verify":
        verify(sys.argv[2], sys.argv[3], sys.argv[4]); return
    fail("fixed command required")


if __name__ == "__main__":
    main()
