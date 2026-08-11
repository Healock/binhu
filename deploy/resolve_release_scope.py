#!/usr/bin/env python3
"""Resolve a requested production release scope without weakening safety."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REQUESTED_SCOPES = {"auto", "backend", "frontend", "full"}
NEUTRAL_FILES = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "VERSION",
}
NEUTRAL_PREFIXES = (
    ".github/",
    "docs/",
)
BACKEND_PREFIX = "backend/"
FRONTEND_PREFIX = "frontend/"


def _git(repository: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *args],
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()


def classify_release_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    if normalized in NEUTRAL_FILES or normalized.startswith(NEUTRAL_PREFIXES):
        return "neutral"
    if normalized.startswith(BACKEND_PREFIX):
        return "backend"
    if normalized.startswith(FRONTEND_PREFIX):
        return "frontend"
    return "full"


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
    classifications = {path: classify_release_path(path) for path in paths}
    runtime_scopes = {
        classification
        for classification in classifications.values()
        if classification != "neutral"
    }

    if requested_scope in {"backend", "frontend"}:
        unsafe_paths = [
            path
            for path, classification in classifications.items()
            if classification not in {"neutral", requested_scope}
        ]
        if unsafe_paths:
            raise ValueError(
                f"{requested_scope} release contains files that require another release scope: "
                + ", ".join(unsafe_paths[:20])
            )
        return requested_scope, []

    if not runtime_scopes:
        # Version, documentation and workflow-only releases still need the
        # backend container recreated so the mounted VERSION becomes active.
        return "backend", []
    if runtime_scopes == {"backend"}:
        return "backend", []
    if runtime_scopes == {"frontend"}:
        return "frontend", []
    full_paths = [
        path
        for path, classification in classifications.items()
        if classification == "full"
    ]
    if full_paths:
        return "full", full_paths
    return "full", paths


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
