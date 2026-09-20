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


STATE = Path("/var/lib/binhu-staging-event-pipeline")
CANDIDATES = STATE / "candidates"
RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BYTES = 128 * 1024 * 1024
MEMBERS = frozenset({"source.tar.gz", "pipeline-job.jar", "manifest.json", "SHA256SUMS"})
EVIDENCE_MEMBERS = frozenset({
    "load-report.json", "resource-samples.json", "rollback-report.json", "manual-report.json",
})


def fail(message: str) -> None:
    raise SystemExit("Staging event-pipeline gateway refused: " + message)


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


def run_module(run_id: str, module: str, manifest: dict | None = None) -> dict:
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
    result = subprocess.run(
        args, cwd=source, env=env, check=True, capture_output=True, text=True,
        timeout=900 if module == "apply" else 300,
    )
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


def control(run_id: str, action: str) -> None:
    if not RUN_RE.fullmatch(run_id) or not (CANDIDATES / run_id).is_dir():
        fail("prepared Staging candidate required")
    result = run_module(run_id, action)
    manifest = json.loads(((CANDIDATES / run_id) / "manifest.json").read_text(encoding="utf-8"))
    if action == "apply":
        manifest.update({"started": True, "acceptance": "pending"})
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


def main() -> None:
    if os.geteuid() != 0:
        fail("root execution required")
    if len(sys.argv) == 2 and sys.argv[1] == "status":
        status(); return
    if len(sys.argv) == 6 and sys.argv[1] == "prepare":
        prepare(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]); return
    if len(sys.argv) == 3 and sys.argv[1] in {"measure", "apply", "sample", "rollback"}:
        control(sys.argv[2], sys.argv[1]); return
    if len(sys.argv) == 3 and sys.argv[1] == "run":
        seed(sys.argv[2]); return
    if len(sys.argv) == 5 and sys.argv[1] == "verify":
        verify(sys.argv[2], sys.argv[3], sys.argv[4]); return
    fail("fixed command required")


if __name__ == "__main__":
    main()
