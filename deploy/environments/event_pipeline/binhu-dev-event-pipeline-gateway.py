#!/usr/bin/env python3
"""Root-side implementation of the fixed Dev event-pipeline gateway.

The gateway accepts an immutable candidate over stdin, keeps it in a private
state directory, and only invokes the checked-in prepare/measure/apply
modules.  It has no production or staging paths and never accepts arbitrary
commands.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

STATE = Path("/var/lib/binhu-dev-event-pipeline")
SOURCE = STATE / "source"
PROJECT = "binhu-development-pipeline"
MAX_BYTES = 128 * 1024 * 1024
RUN_RE = re.compile(r"^dev-[0-9]{8}-[A-Za-z0-9][A-Za-z0-9_-]{3,31}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> None:
    raise SystemExit(f"Dev event-pipeline gateway refused: {message}")


def safe_member(member: tarfile.TarInfo) -> bool:
    name = Path(member.name)
    return member.isfile() and not member.issym() and not member.islnk() and not name.is_absolute() and ".." not in name.parts


def read_bundle(size: int, expected: str, destination: Path) -> None:
    if size <= 0 or size > MAX_BYTES:
        fail("bundle size outside limit")
    digest = hashlib.sha256()
    remaining = size
    with destination.open("wb") as out:
        while remaining:
            chunk = sys.stdin.buffer.read(min(1024 * 1024, remaining))
            if not chunk:
                fail("incomplete candidate bundle")
            out.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    if digest.hexdigest() != expected:
        fail("candidate bundle hash mismatch")


def bundle_manifest(bundle: Path) -> dict:
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        if {m.name for m in members} != {"binhu-dev-event-pipeline.tar.gz", "SHA256SUMS"}:
            # The Actions artifact contains the candidate bundle and checksum;
            # accepting a raw bundle keeps the gateway useful for controlled
            # replay while still requiring the inner deploy.py contract.
            if {m.name for m in members} != {"event_pipeline-source.tar.gz", "manifest.json", "SHA256SUMS"}:
                fail("candidate archive contents are not fixed")
            return _manifest_from_payload(archive, members)
        inner = archive.extractfile("binhu-dev-event-pipeline.tar.gz")
        if inner is None:
            fail("candidate bundle missing")
        data = inner.read()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    try:
        with tarfile.open(path, "r:gz") as nested:
            return _manifest_from_payload(nested, nested.getmembers())
    finally:
        path.unlink(missing_ok=True)


def _manifest_from_payload(archive: tarfile.TarFile, members: list[tarfile.TarInfo]) -> dict:
    if any(not safe_member(m) for m in members):
        fail("unsafe candidate archive entry")
    item = archive.extractfile("manifest.json")
    if item is None:
        fail("candidate manifest missing")
    try:
        payload = json.loads(item.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("candidate manifest invalid")
    if payload.get("environment") != "development" or payload.get("project") != PROJECT:
        fail("candidate identity mismatch")
    if payload.get("ready_for_dev_pipeline") is not True or payload.get("started") is not False:
        fail("candidate is not pending")
    if not COMMIT_RE.fullmatch(str(payload.get("commit", ""))) or not RUN_RE.fullmatch(str(payload.get("run_id", ""))):
        fail("candidate commit or run id invalid")
    images = payload.get("images")
    if not isinstance(images, dict) or set(images) != {"mysql", "redis", "worker", "flink"} or any(not SHA_RE.fullmatch(str(v).removeprefix("sha256:")) for v in images.values()):
        fail("candidate image identities incomplete")
    return payload


def status() -> None:
    current = STATE / "current.json"
    if current.is_file():
        print(current.read_text(encoding="utf-8"))
    else:
        print(json.dumps({"environment": "development", "project": PROJECT, "status": "empty"}))


def prepare(run_id: str, commit: str, size: str, digest: str) -> None:
    if not RUN_RE.fullmatch(run_id) or not COMMIT_RE.fullmatch(commit) or not SHA_RE.fullmatch(digest):
        fail("invalid prepare identity")
    target = STATE / "candidates" / run_id
    if target.exists():
        fail("run id already exists")
    target.mkdir(mode=0o700, parents=True)
    bundle = target / "candidate.tar.gz"
    try:
        read_bundle(int(size), digest, bundle)
        manifest = bundle_manifest(bundle)
        if manifest["run_id"] != run_id or manifest["commit"] != commit:
            fail("command and manifest identity differ")
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(target / "manifest.json", 0o600)
        # Generate the server-side Compose and identity files only through the
        # checked-in prepare module; no candidate may provide executable hooks.
        run_module(run_id, "prepare")
        print(json.dumps({"environment": "development", "project": PROJECT, "run_id": run_id, "prepared": True}))
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def run_module(run_id: str, module: str) -> None:
    candidate = STATE / "candidates" / run_id
    manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
    with tarfile.open(candidate / "candidate.tar.gz", "r:gz") as outer:
        names = {m.name for m in outer.getmembers()}
        inner_name = "binhu-dev-event-pipeline.tar.gz" if "binhu-dev-event-pipeline.tar.gz" in names else None
        stream = outer.extractfile(inner_name or "event_pipeline-source.tar.gz")
        if stream is None:
            fail("source archive missing")
        source_bytes = stream.read()
    SOURCE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=STATE, delete=False) as tmp:
        tmp.write(source_bytes)
        source_archive = Path(tmp.name)
    try:
        shutil.rmtree(SOURCE / "deploy", ignore_errors=True)
        with tarfile.open(source_archive, "r:gz") as source:
            if any(not safe_member(m) or not m.name.startswith("deploy/environments/event_pipeline/") for m in source.getmembers()):
                fail("unsafe source archive")
            source.extractall(SOURCE)
        env = os.environ.copy()
        env.update({"DEV_RUN_ID": run_id, "APP_ENVIRONMENT": "development"})
        config = Path("/etc/binhu-dev-event-pipeline.conf")
        if config.is_file():
            for line in config.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    if re.fullmatch(r"[A-Z0-9_]+", key):
                        env[key] = value
        env["PYTHONPATH"] = str(SOURCE / "deploy" / "environments")
        if module == "prepare":
            args = [sys.executable, "-m", "event_pipeline.prepare", "--run-id", run_id]
            for key in ("mysql", "redis", "worker", "flink"):
                args += [f"--{key}-image", manifest["images"][key]]
        else:
            args = [sys.executable, "-m", "event_pipeline.control", module]
        subprocess.run(args, cwd=SOURCE, env=env, check=True, timeout=600)
    finally:
        source_archive.unlink(missing_ok=True)


def measure(run_id: str) -> None:
    candidate = STATE / "candidates" / run_id
    if not candidate.is_dir():
        fail("candidate not prepared")
    run_module(run_id, "measure")
    print(json.dumps({"environment": "development", "project": PROJECT, "run_id": run_id, "measured": True}))


def apply(run_id: str) -> None:
    candidate = STATE / "candidates" / run_id
    if not candidate.is_dir():
        fail("candidate not prepared")
    run_module(run_id, "apply")
    payload = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
    payload.update({"started": True, "acceptance": "pending"})
    (STATE / "current.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(STATE / "current.json", 0o600)
    print(json.dumps({"environment": "development", "project": PROJECT, "run_id": run_id, "applied": True, "acceptance": "pending"}))


def main() -> None:
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "status" and len(sys.argv) == 2:
        status()
    elif action == "prepare" and len(sys.argv) == 6:
        prepare(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif action in {"measure", "apply"} and len(sys.argv) == 3:
        (measure if action == "measure" else apply)(sys.argv[2])
    else:
        fail("fixed prepare/measure/apply contract required")


if __name__ == "__main__":
    main()
