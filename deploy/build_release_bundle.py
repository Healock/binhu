#!/usr/bin/env python3
"""Build a self-contained, checksum-protected production release bundle."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
BACKUP_SCOPES = {"none", "online", "daily", "all"}
RELEASE_SCOPES = {"backend", "full"}
BUNDLE_SCHEMA = 2


def _run_git(repository: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repository), *args],
        stderr=subprocess.STDOUT,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_dist(frontend_dist: Path) -> None:
    if not frontend_dist.is_dir() or not (frontend_dist / "index.html").is_file():
        raise ValueError("frontend dist must contain index.html")
    for path in frontend_dist.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"frontend dist cannot contain symlinks: {path}")


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(payload))


def build_bundle(
    repository: Path,
    frontend_dist: Path | None,
    commit: str,
    backup_scope: str,
    release_scope: str,
    output: Path,
) -> dict:
    repository = repository.resolve()
    commit = _run_git(repository, "rev-parse", f"{commit}^{{commit}}").decode().strip()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ValueError("commit must resolve to a full SHA-1 commit id")
    if backup_scope not in BACKUP_SCOPES:
        raise ValueError(f"unsupported backup scope: {backup_scope}")
    if release_scope not in RELEASE_SCOPES:
        raise ValueError(f"unsupported release scope: {release_scope}")

    version = _run_git(repository, "show", f"{commit}:VERSION").decode().strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"invalid VERSION at {commit}: {version!r}")
    if release_scope == "full":
        if frontend_dist is None:
            raise ValueError("full release requires frontend dist")
        frontend_dist = frontend_dist.resolve()
        _validate_dist(frontend_dist)

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="binhu-release-") as temporary:
        work = Path(temporary)
        source_archive = work / "source.tar.gz"
        source_command = [
            "git",
            "-C",
            str(repository),
            "archive",
            "--format=tar.gz",
            f"--output={source_archive}",
            commit,
        ]
        if release_scope == "backend":
            source_command.extend(["VERSION", "backend"])
        subprocess.run(source_command, check=True)

        files = {
            source_archive.name: {
                "sha256": _sha256(source_archive),
                "size": source_archive.stat().st_size,
            },
        }
        frontend_archive = work / "frontend-dist.tar.gz"
        if release_scope == "full":
            with tarfile.open(frontend_archive, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                archive.add(frontend_dist, arcname="dist", recursive=True)
            files[frontend_archive.name] = {
                "sha256": _sha256(frontend_archive),
                "size": frontend_archive.stat().st_size,
            }
        manifest = {
            "schema": BUNDLE_SCHEMA,
            "version": version,
            "commit": commit,
            "backup_scope": backup_scope,
            "release_scope": release_scope,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": files,
        }
        manifest_bytes = (json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n").encode("utf-8")
        checksums = "".join(
            f"{metadata['sha256']}  {name}\n"
            for name, metadata in sorted(files.items())
        ).encode("ascii")

        with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            bundle.add(source_archive, arcname=source_archive.name, recursive=False)
            if release_scope == "full":
                bundle.add(frontend_archive, arcname=frontend_archive.name, recursive=False)
            _add_bytes(bundle, "manifest.json", manifest_bytes)
            _add_bytes(bundle, "SHA256SUMS", checksums)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--frontend-dist", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--backup-scope", choices=sorted(BACKUP_SCOPES), required=True)
    parser.add_argument("--release-scope", choices=sorted(RELEASE_SCOPES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_bundle(
        args.repository,
        args.frontend_dist,
        args.commit,
        args.backup_scope,
        args.release_scope,
        args.output,
    )
    print(json.dumps({
        "version": manifest["version"],
        "commit": manifest["commit"],
        "backup_scope": manifest["backup_scope"],
        "release_scope": manifest["release_scope"],
        "bundle": str(args.output.resolve()),
        "sha256": _sha256(args.output.resolve()),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
