"""Validate a Staging acceptance against its protected server-side ledger."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from .artifact import extract_source

from .artifact import file_hash, verify_artifact
from .image import inspect_image, verify_image

STAGING_ROOT = Path("/var/lib/binhu-staging-application/candidates")
PRODUCTION_ROOT = Path("/var/lib/binhu-deploy/candidates")
RUN_RE = re.compile(r"^staging-app-[0-9a-f]{16}$")
ARTIFACT_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ACCEPTED_FIELDS = (
    "environment", "run_id", "artifact_id", "image_id", "commit", "version",
    "dev_acceptance_run_id", "accepted", "production_modified",
)


def _json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("staging_ledger_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("staging_ledger_invalid")
    return value


def _identity(run_id: str, artifact_id: str, artifact_sha256: str,
              image_id: str, commit: str, version: str) -> None:
    values = (run_id, artifact_id, artifact_sha256, image_id, commit, version)
    if (not all(isinstance(value, str) for value in values)
            or not RUN_RE.fullmatch(run_id) or not ARTIFACT_RE.fullmatch(artifact_id)
            or not SHA_RE.fullmatch(artifact_sha256) or not IMAGE_RE.fullmatch(image_id)
            or not COMMIT_RE.fullmatch(commit) or not VERSION_RE.fullmatch(version)):
        raise ValueError("promotion_identity_invalid")


def _accepted_ledger(candidate: Path, *, run_id: str, artifact_id: str,
                     artifact_sha256: str, image_id: str, commit: str, version: str) -> dict:
    ledger = _json(candidate / "promotion.json")
    accepted = _json(candidate / "accept.json")
    expected = {
        "environment": "staging", "run_id": run_id, "artifact_id": artifact_id,
        "image_id": image_id, "commit": commit, "version": version,
        "artifact_manifest_sha256": artifact_sha256, "state": "accepted",
    }
    if any(ledger.get(key) != value for key, value in expected.items()):
        raise ValueError("staging_ledger_not_accepted_or_mismatch")
    expected_acceptance = {key: ledger.get(key) for key in ACCEPTED_FIELDS
                           if key not in {"accepted", "production_modified"}}
    expected_acceptance.update({"accepted": True, "production_modified": False})
    if accepted != expected_acceptance:
        raise ValueError("staging_acceptance_record_mismatch")
    return ledger


def _existing_promotion(target: Path, *, run_id: str, artifact_id: str,
                        artifact_sha256: str, image_id: str, commit: str,
                        version: str, compose_sha256: str) -> dict:
    if target.is_symlink() or not target.is_dir():
        raise ValueError("production_candidate_invalid")
    manifest_path = target / "promotion.json"
    manifest = _json(manifest_path)
    expected = {
        "environment": "production-promotion", "staging_run_id": run_id,
        "artifact_id": artifact_id, "artifact_manifest_sha256": artifact_sha256,
        "image_id": image_id, "commit": commit, "version": version,
        "production_config_commit": commit,
        "production_compose_sha256": compose_sha256,
        "staging_ledger_state": "accepted",
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("production_candidate_identity_conflict")
    actual = {}
    for path in target.rglob("*"):
        if path.is_symlink():
            raise ValueError("production_candidate_invalid")
        if path.is_file() and path != manifest_path:
            actual[path.relative_to(target).as_posix()] = {
                "sha256": file_hash(path), "size": path.stat().st_size,
            }
    if manifest.get("files") != actual:
        raise ValueError("production_candidate_digest_mismatch")
    return manifest


def save_accepted_image(image_id: str, archive: Path) -> dict:
    """Save the Staging-accepted daemon image into the immutable promotion candidate."""
    if not IMAGE_RE.fullmatch(image_id):
        raise ValueError("staging_image_identity_invalid")
    archive = Path(archive)
    saved = subprocess.run(["docker", "image", "save", "--output", str(archive), image_id],
                           capture_output=True, check=False, timeout=900)
    if saved.returncode or not archive.is_file() or archive.stat().st_size == 0:
        raise ValueError("staging_image_save_failed")
    archive.chmod(0o600)
    image = inspect_image(image_id)
    if image.get("Id") != image_id:
        raise ValueError("staging_image_digest_mismatch")
    return image


def promote_accepted_candidate(*, run_id: str, artifact_id: str, artifact_sha256: str,
                               image_id: str, commit: str, version: str,
                               production_compose: Path, production_compose_sha256: str,
                               staging_root: Path = STAGING_ROOT,
                               production_root: Path = PRODUCTION_ROOT) -> dict:
    _identity(run_id, artifact_id, artifact_sha256, image_id, commit, version)
    staging_root = Path(staging_root).absolute()
    production_root = Path(production_root).absolute()
    if (staging_root.is_symlink() or production_root.is_symlink()
            or any(p.is_symlink() for p in staging_root.parents)
            or any(p.is_symlink() for p in production_root.parents)):
        raise ValueError("promotion_root_invalid")
    candidate = staging_root / run_id
    ledger = _accepted_ledger(candidate, run_id=run_id, artifact_id=artifact_id,
                              artifact_sha256=artifact_sha256, image_id=image_id,
                              commit=commit, version=version)
    artifact_dir = candidate / "artifact"
    artifact = verify_artifact(artifact_dir)
    if (artifact.get("artifact_id") != artifact_id or artifact.get("commit") != commit
            or artifact.get("version") != version
            or file_hash(artifact_dir / "artifact.json") != artifact_sha256
            or ledger.get("artifact_manifest_sha256") != artifact_sha256):
        raise ValueError("staging_artifact_identity_mismatch")
    production_compose = Path(production_compose)
    if (production_compose.is_symlink() or not production_compose.is_file()
            or not SHA_RE.fullmatch(production_compose_sha256)
            or file_hash(production_compose) != production_compose_sha256):
        raise ValueError("production_compose_identity_mismatch")
    compose_text = production_compose.read_text(encoding="utf-8")
    if (re.search(r"(?m)^\s*build\s*:", compose_text)
            or not re.search(r"(?m)^\s*image\s*:\s*binhu-backend\s*$", compose_text)):
        raise ValueError("production_compose_contract_invalid")
    image = verify_image(artifact_dir, artifact_id, candidate / "image")
    if image.get("image_id") != image_id:
        raise ValueError("staging_image_manifest_mismatch")
    production_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = production_root / run_id
    if target.exists():
        return _existing_promotion(target, run_id=run_id, artifact_id=artifact_id,
                                   artifact_sha256=artifact_sha256, image_id=image_id,
                                   commit=commit, version=version,
                                   compose_sha256=production_compose_sha256)
    if target.is_symlink():
        raise ValueError("production_candidate_invalid")
    temporary_target = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=production_root))
    temporary_target.chmod(0o700)
    try:
        saved_image = save_accepted_image(image_id, temporary_target / "image.tar")
        if saved_image.get("Id") != image_id:
            raise ValueError("staging_image_digest_mismatch")
        labels = saved_image.get("Config", {}).get("Labels", {})
        if (labels.get("binhu.artifact.id") != artifact_id
                or labels.get("org.opencontainers.image.revision") != commit
                or labels.get("org.opencontainers.image.version") != version):
            raise ValueError("staging_image_labels_mismatch")
        copied = temporary_target / "artifact"
        copied.mkdir(mode=0o700)
        for name in ("artifact.json", "source.tar", "frontend.tar"):
            shutil.copyfile(artifact_dir / name, copied / name)
        shutil.copyfile(candidate / "image.json", temporary_target / "image.json")
        shutil.copyfile(candidate / "accept.json", temporary_target / "accept.json")
        shutil.copyfile(candidate / "promotion.json", temporary_target / "staging-ledger.json")
        config = temporary_target / "production-config"
        config.mkdir(mode=0o700)
        shutil.copyfile(production_compose, config / "docker-compose.yml")
        files = {}
        for path in sorted(p for p in temporary_target.rglob("*") if p.is_file()):
            relative = path.relative_to(temporary_target).as_posix()
            path.chmod(0o600)
            files[relative] = {"sha256": file_hash(path), "size": path.stat().st_size}
        manifest = {
            "schema": 1, "environment": "production-promotion",
            "staging_run_id": run_id, "artifact_id": artifact_id,
            "artifact_manifest_sha256": artifact_sha256, "image_id": image_id,
            "commit": commit, "version": version,
            "production_config_commit": commit,
            "production_compose_sha256": production_compose_sha256,
            "staging_ledger_state": "accepted", "files": files,
        }
        manifest_path = temporary_target / "promotion.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        source_dir = temporary_target / "source"
        source_dir.mkdir(mode=0o700)
        extract_source(copied / "source.tar", source_dir)
        shutil.copyfile(config / "docker-compose.yml", source_dir / "docker-compose.yml")
        source_archive = temporary_target / "source.tar.gz"
        frontend_archive = temporary_target / "frontend-dist.tar.gz"
        with tarfile.open(source_archive, "w:gz") as archive:
            for path in sorted(item for item in source_dir.rglob("*") if item.is_file()):
                archive.add(path, arcname=path.relative_to(source_dir).as_posix(), recursive=False)
        with (copied / "frontend.tar").open("rb") as source, frontend_archive.open("wb") as output:
            with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
        files = {
            source_archive.name: {"sha256": file_hash(source_archive), "size": source_archive.stat().st_size},
            frontend_archive.name: {"sha256": file_hash(frontend_archive), "size": frontend_archive.stat().st_size},
        }
        checksums = "".join(f"{item['sha256']}  {name}\n" for name, item in sorted(files.items()))
        release = {
            "schema": 3, "version": version, "commit": commit,
            "backup_scope": "all", "release_scope": "full",
            "release_kind": "staging-promotion", "staging_run_id": run_id,
            "artifact_id": artifact_id, "artifact_manifest_sha256": artifact_sha256,
            "image_id": image_id, "production_config_commit": commit,
            "production_compose_sha256": production_compose_sha256,
            "files": files,
        }
        (temporary_target / "manifest.json").write_text(
            json.dumps(release, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        (temporary_target / "SHA256SUMS").write_text(checksums, encoding="ascii")
        bundle = temporary_target / "production-release.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            for name in (source_archive.name, frontend_archive.name, "manifest.json", "SHA256SUMS"):
                archive.add(temporary_target / name, arcname=name, recursive=False)
        bundle.chmod(0o600)
        manifest["release_bundle_sha256"] = file_hash(bundle)
        manifest["files"] = {
            path.relative_to(temporary_target).as_posix(): {
                "sha256": file_hash(path), "size": path.stat().st_size,
            }
            for path in sorted(temporary_target.rglob("*"))
            if path.is_file() and path != manifest_path
        }
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.rename(temporary_target, target)
        return manifest
    except Exception:
        shutil.rmtree(temporary_target, ignore_errors=True)
        raise


def promote_accepted_run(*, run_id: str, expected_commit: str, expected_version: str,
                         production_compose: Path, production_compose_sha256: str,
                         staging_root: Path = STAGING_ROOT,
                         production_root: Path = PRODUCTION_ROOT) -> dict:
    if not RUN_RE.fullmatch(run_id):
        raise ValueError("promotion_identity_invalid")
    staging_root = Path(staging_root).absolute()
    candidate = staging_root / run_id
    if (staging_root.is_symlink() or candidate.is_symlink() or not candidate.is_dir()
            or any(path.is_symlink() for path in staging_root.parents)):
        raise ValueError("staging_candidate_invalid")
    ledger = _json(candidate / "promotion.json")
    if ledger.get("commit") != expected_commit or ledger.get("version") != expected_version:
        raise ValueError("staging_ledger_not_accepted_or_mismatch")
    return promote_accepted_candidate(
        run_id=run_id,
        artifact_id=ledger.get("artifact_id", ""),
        artifact_sha256=ledger.get("artifact_manifest_sha256", ""),
        image_id=ledger.get("image_id", ""),
        commit=expected_commit,
        version=expected_version,
        production_compose=production_compose,
        production_compose_sha256=production_compose_sha256,
        staging_root=staging_root,
        production_root=production_root,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("expected_commit")
    parser.add_argument("expected_version")
    parser.add_argument("production_compose", type=Path)
    parser.add_argument("production_compose_sha256")
    args = parser.parse_args()
    try:
        result = promote_accepted_run(**vars(args))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Staging promotion rejected; inspect private deployment evidence") from None
    print(json.dumps({key: result[key] for key in (
        "staging_run_id", "artifact_id", "artifact_manifest_sha256", "image_id", "commit", "version")},
        sort_keys=True))


if __name__ == "__main__":
    main()
