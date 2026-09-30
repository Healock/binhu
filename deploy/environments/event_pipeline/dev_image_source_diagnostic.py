#!/usr/bin/env python3
"""Read-only, allow-listed source metadata for the Dev registry image.

The result intentionally excludes environment/config contents and container
logs.  It is used to identify an approved immutable source before Staging can
preload a missing image.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

SERVICE = "schema-registry"
PROJECT = "binhu-development-eventbus"
SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REF_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?"
    r"(?:@sha256:[0-9a-f]{64})?$"
)
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?$")


def _inspect() -> dict:
    result = subprocess.run(
        [
            "docker", "ps", "-aq",
            "--filter", f"label=com.docker.compose.project={PROJECT}",
            "--filter", f"label=com.docker.compose.service={SERVICE}",
        ], capture_output=True, text=True, timeout=30, check=True,
    )
    ids = [item for item in result.stdout.splitlines() if item]
    if len(ids) != 1:
        raise ValueError("schema registry container identity mismatch")
    inspected = subprocess.run(
        ["docker", "inspect", ids[0]], capture_output=True, text=True,
        timeout=30, check=True,
    )
    payload = json.loads(inspected.stdout)
    if len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError("schema registry inspection invalid")
    return payload[0]


def diagnose() -> dict:
    item = _inspect()
    config = item.get("Config") or {}
    image_id = str(item.get("Image") or "")
    if not SHA_RE.fullmatch(image_id):
        raise ValueError("schema registry image identity invalid")
    source = str(config.get("Image") or "")
    if source and not IMAGE_REF_RE.fullmatch(source):
        raise ValueError("schema registry image source invalid")
    repo_digests = sorted({
        value for value in (item.get("RepoDigests") or [])
        if isinstance(value, str) and "@" in value
        and SHA_RE.fullmatch(value.rsplit("@", 1)[1])
    })
    repo_tags = sorted({
        value for value in (item.get("RepoTags") or [])
        if isinstance(value, str) and TAG_RE.fullmatch(value)
    })
    return {
        "environment": "development",
        "project": PROJECT,
        "service": SERVICE,
        "image_id": image_id,
        "image_source": source,
        "repo_digests": repo_digests,
        "repo_tags": repo_tags,
        "status": "passed",
    }


if __name__ == "__main__":
    try:
        print(json.dumps(diagnose(), sort_keys=True))
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        print(json.dumps({"environment": "development", "service": SERVICE,
                          "status": "failed", "error_code": "dev_image_source_unavailable"}))
        raise SystemExit(1)
