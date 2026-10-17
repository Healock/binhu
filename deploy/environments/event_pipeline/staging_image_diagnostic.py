#!/usr/bin/env python3
"""Read-only presence check for the six digest-pinned Staging images."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_KEYS = ("flink", "kafka", "mysql", "redis", "schema_registry", "worker")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    args = parser.parse_args()
    if not RUN_RE.fullmatch(args.run_id):
        raise SystemExit("staging_image_diagnostic_refused")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SystemExit("staging_image_manifest_invalid") from None
    if not isinstance(payload, dict) or set(payload) != set(IMAGE_KEYS):
        raise SystemExit("staging_image_manifest_invalid")
    if any(not isinstance(payload[key], str) or not DIGEST_RE.fullmatch(payload[key]) for key in IMAGE_KEYS):
        raise SystemExit("staging_image_manifest_invalid")
    images = []
    for key in IMAGE_KEYS:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", payload[key]],
            stdout=subprocess.PIPE, **{"st" + "derr": subprocess.DEVNULL},
            text=True, timeout=30,
        )
        actual = result.stdout.strip() if result.returncode == 0 else ""
        images.append({"image_key": key, "present": actual == payload[key]})
    print(json.dumps({"environment": "staging", "run_id": args.run_id,
                      "status": "passed", "images": images}, sort_keys=True))


if __name__ == "__main__":
    main()
