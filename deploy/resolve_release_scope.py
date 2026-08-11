#!/usr/bin/env python3
"""Resolve a requested production release scope without weakening safety."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REQUESTED_SCOPES = {"auto", "backend", "full"}
BACKEND_ONLY_FILES = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "VERSION",
}
BACKEND_ONLY_PREFIXES = (
    ".github/",
    "backend/",
    "docs/",
)


def _git(repository: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *args],
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()


def is_backend_only_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    return normalized in BACKEND_ONLY_FILES or normalized.startswith(BACKEND_ONLY_PREFIXES)


def changed_paths(repository: Path, deployed_commit: str, release_commit: str) -> list[str]:
    repository = repository.resolve()
    if not COMMIT_PATTERN.fullmatch(deployed_commit):
        raise ValueError("deployed commit is unavailable")
    if not COMMIT_PATTERN.fullmatch(release_commit):
        raise ValueError("release commit is invalid")
    try:
        subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", deployed_commit, release_commit],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError("deployed commit is not an ancestor of the release") from exc
    output = _git(repository, "diff", "--name-only", f"{deployed_commit}..{release_commit}")
    return [line.strip() for line in output.splitlines() if line.strip()]


def resolve_release_scope(
    repository: Path,
    requested_scope: str,
    deployed_commit: str,
    release_commit: str,
) -> tuple[str, list[str]]:
    if requested_scope not in REQUESTED_SCOPES:
        raise ValueError("unsupported requested release scope")
    if requested_scope == "full":
        return "full", []
    try:
        paths = changed_paths(repository, deployed_commit, release_commit)
    except ValueError:
        if requested_scope == "auto":
            # Without a trusted production ancestor we cannot prove that a
            # backend-only archive is complete. Fall back to a full archive.
            return "full", ["production commit could not be verified"]
        raise
    unsafe_paths = [path for path in paths if not is_backend_only_path(path)]
    if unsafe_paths:
        if requested_scope == "backend":
            raise ValueError(
                "backend release contains files that require a full release: "
                + ", ".join(unsafe_paths[:20])
            )
        return "full", unsafe_paths
    return "backend", []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--requested", choices=sorted(REQUESTED_SCOPES), required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--release-commit", required=True)
    args = parser.parse_args()
    scope, unsafe_paths = resolve_release_scope(
        args.repository,
        args.requested,
        args.deployed_commit,
        args.release_commit,
    )
    print(scope)
    if unsafe_paths:
        print("full release required by: " + ", ".join(unsafe_paths[:20]), file=sys.stderr)


if __name__ == "__main__":
    main()
