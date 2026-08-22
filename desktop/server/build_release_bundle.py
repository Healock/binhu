#!/usr/bin/env python3
"""Create the validated two-platform bundle consumed by the SSH gateway."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

PLATFORMS = ("win7-x64", "win10-x64")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = {"schemaVersion": 1, "version": args.version, "commit": args.commit, "signed": False, "platforms": {}}
    for platform in PLATFORMS:
        root = args.input / platform
        if not (root / "releases.stable.json").is_file():
            raise SystemExit(f"missing release feed: {root}")
        files = []
        for path in sorted(item for item in root.iterdir() if item.is_file()):
            files.append({"name": path.name, "size": path.stat().st_size, "sha256": sha256(path)})
        manifest["platforms"][platform] = {"files": files}
    manifest_path = args.input / "release.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz") as archive:
        archive.add(manifest_path, arcname="release.json")
        for platform in PLATFORMS:
            for path in sorted(item for item in (args.input / platform).iterdir() if item.is_file()):
                archive.add(path, arcname=f"{platform}/{path.name}")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
