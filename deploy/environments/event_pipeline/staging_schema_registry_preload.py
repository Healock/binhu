#!/usr/bin/env python3
"""Load one approved Schema Registry image into Staging without starting it."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
APPROVED_IMAGE = "sha256:cac9355ce4daf5fd4c027c1f213577c2af41dc7c3bcd10cd9f1807a53531a2b4"
EVIDENCE_ROOT = Path("/var/lib/binhu-staging-event-pipeline/evidence")
MAX_BYTES = 4 * 1024 * 1024 * 1024


def _result(run_id: str, **fields: object) -> dict:
    return {"environment": "staging", "run_id": run_id, **fields}


def _write_once(path: Path, payload: dict) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("preload evidence already exists")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def preload(run_id: str, image_id: str) -> dict:
    if not RUN_RE.fullmatch(run_id) or image_id != APPROVED_IMAGE or not DIGEST_RE.fullmatch(image_id):
        raise ValueError("fixed preload identity required")
    evidence = EVIDENCE_ROOT / run_id
    if evidence.exists() or evidence.is_symlink():
        raise ValueError("preload run already exists")
    evidence.mkdir(mode=0o700, parents=True)
    archive = evidence / "schema-registry-image.tar.partial"
    report = evidence / "schema-registry-preload.json"
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open("xb") as output:
            while True:
                block = sys.stdin.buffer.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > MAX_BYTES:
                    raise ValueError("image archive exceeds fixed limit")
                output.write(block)
                digest.update(block)
        if size == 0:
            raise ValueError("image archive is empty")
        loaded = subprocess.run(
            ["docker", "load", "--input", str(archive)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=900,
        )
        if loaded.returncode:
            raise RuntimeError("docker image load failed")
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image_id],
            capture_output=True, text=True, timeout=30,
        )
        actual = inspected.stdout.strip() if inspected.returncode == 0 else ""
        if actual != image_id:
            raise ValueError("loaded image identity mismatch")
        payload = _result(
            run_id,
            source_environment="development",
            image_key="schema_registry",
            expected_image_id=image_id,
            loaded_image_id=actual,
            archive_sha256=digest.hexdigest(),
            archive_size=size,
            started=False,
            status="passed",
        )
        _write_once(report, payload)
        return payload
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        payload = _result(run_id, source_environment="development", image_key="schema_registry",
                          expected_image_id=image_id, status="failed",
                          error_code=("image_archive_too_large" if "exceeds" in str(error)
                                      else "schema_registry_preload_failed"))
        try:
            _write_once(report, payload)
        except OSError:
            pass
        raise
    finally:
        archive.unlink(missing_ok=True)


def main() -> None:
    if os.geteuid() != 0 or len(sys.argv) != 3:
        raise SystemExit("staging_schema_registry_preload_refused")
    try:
        print(json.dumps(preload(sys.argv[1], sys.argv[2]), sort_keys=True))
    except Exception:
        # The report contains only fixed identity fields and a bounded error code.
        # Echo it so the controlled workflow can diagnose a failed preload without
        # exposing docker output, credentials, or archive contents.
        try:
            report = EVIDENCE_ROOT / sys.argv[1] / "schema-registry-preload.json"
            if report.is_file() and not report.is_symlink():
                print(report.read_text(encoding="utf-8"), file=sys.stderr, end="")
        except (OSError, UnicodeDecodeError, IndexError):
            pass
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
