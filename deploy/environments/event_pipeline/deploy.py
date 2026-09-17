"""Build and verify an immutable Dev event-pipeline candidate bundle.

This module deliberately stops at artifact preparation.  It does not connect to
Docker, SSH, Kafka, MySQL, or a deployment gateway.  A later, separately
reviewed Dev gateway may consume the bundle after its target and rollback
contract have been installed on the server.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


PROJECT = "binhu-development-pipeline"
ENVIRONMENT = "development"
RUN_ID_RE = re.compile(r"dev-[0-9]{8}-[A-Za-z0-9][A-Za-z0-9_-]{3,31}\Z")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
SOURCE_ROOT = Path("deploy/environments/event_pipeline")
SOURCE_ARCHIVE = "event_pipeline-source.tar.gz"
MANIFEST = "manifest.json"
CHECKSUMS = "SHA256SUMS"
PIPELINE_JAR = "pipeline-job.jar"
PIPELINE_SOURCE = "deploy/environments/event_pipeline/PipelineJob.java"
ALLOWED_TOP_LEVEL = {SOURCE_ARCHIVE, PIPELINE_JAR, MANIFEST, CHECKSUMS}
FORBIDDEN_TEXT = re.compile(
    r"(?i)(password|secret|token|cookie|authorization|bearer|private_key|mysql_pwd)"
)


def _require_commit(value: str) -> str:
    if not COMMIT_RE.fullmatch(value or ""):
        raise ValueError("full commit SHA required")
    return value


def _require_run_id(value: str) -> str:
    if not RUN_ID_RE.fullmatch(value or ""):
        raise ValueError("fresh Dev run ID required")
    return value


def _require_digest(value: str, label: str = "image") -> str:
    if not SHA256_RE.fullmatch(value or ""):
        raise ValueError(f"immutable {label} digest required (length={len(value or '')})")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files(repository: Path) -> list[Path]:
    root = repository / SOURCE_ROOT
    if not root.is_dir() or root.is_symlink():
        raise ValueError("event pipeline source directory missing")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if "target" in path.relative_to(root).parts:
            continue
        relative = path.relative_to(repository).as_posix()
        if "/__pycache__/" in f"/{relative}/" or relative.endswith(".pyc"):
            continue
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("event pipeline source file too large")
        files.append(path)
    if not files:
        raise ValueError("event pipeline source is empty")
    return files


def _scan_source(files: list[Path]) -> None:
    for path in files:
        if path.suffix not in {".py", ".java", ".md", ".json", ".txt", ".sh", ".xml", ".gitattributes", ""}:
            raise ValueError("unsupported event pipeline source file")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("event pipeline source must be UTF-8") from exc
        if FORBIDDEN_TEXT.search(text) and path.name not in {"README.md", "source-provenance.json"}:
            # Variable names such as MYSQL_PASSWORD are expected in validation
            # code.  Only reject literal credential-looking values; references
            # to environment variables and generated secrets remain allowed.
            literal = re.search(
                r"(?i)(?:password|secret|token|cookie)\s*[:=]\s*['\"][^'\"]{16,}['\"]",
                text,
            ) or re.search(r"(?i)(?:password|secret|token|cookie)\s*[:=]\s*[0-9a-f]{32,}(?:\s|$)", text)
            if literal:
                raise ValueError("candidate source contains a credential value")


def _write_archive(repository: Path, files: list[Path], destination: Path) -> dict[str, str]:
    hashes = {}
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            relative = path.relative_to(repository).as_posix()
            info = archive.gettarinfo(str(path), arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            with path.open("rb") as stream:
                archive.addfile(info, stream)
            hashes[relative] = _sha256(path)
    return hashes


def _safe_member(member: tarfile.TarInfo) -> bool:
    if member.issym() or member.islnk() or not member.isfile():
        return False
    name = Path(member.name)
    return not name.is_absolute() and ".." not in name.parts and member.name.startswith("deploy/environments/event_pipeline/")


def _verify_archive(path: Path, expected_hashes: dict[str, str]) -> None:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if not members or any(not _safe_member(member) for member in members):
            raise ValueError("candidate source archive contains unsafe entries")
        actual = {}
        for member in members:
            content = archive.extractfile(member)
            if content is None:
                raise ValueError("candidate source archive entry unreadable")
            actual[member.name] = hashlib.sha256(content.read()).hexdigest()
    if actual != expected_hashes:
        raise ValueError("candidate source hash manifest mismatch")


def _verify_pipeline_jar(path: Path) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("compiled Dev PipelineJob JAR missing")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "PipelineJob.class" not in names or any(".." in Path(name).parts for name in names):
                raise ValueError("compiled Dev PipelineJob class missing")
    except zipfile.BadZipFile as exc:
        raise ValueError("compiled Dev PipelineJob JAR invalid") from exc


def build(repository: Path, output: Path, *, commit: str, run_id: str, pipeline_jar: Path,
          mysql_image: str, redis_image: str, worker_image: str, flink_image: str) -> dict:
    repository = repository.resolve()
    _require_commit(commit)
    _require_run_id(run_id)
    images = {
        "mysql": _require_digest(mysql_image, "mysql"),
        "redis": _require_digest(redis_image, "redis"),
        "worker": _require_digest(worker_image, "worker"),
        "flink": _require_digest(flink_image, "flink"),
    }
    files = _source_files(repository)
    _scan_source(files)
    output = output.resolve()
    if output.exists() or output.parent == output:
        raise ValueError("candidate output must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dev-pipeline-bundle-") as temporary:
        staging = Path(temporary)
        archive_path = staging / SOURCE_ARCHIVE
        source_hashes = _write_archive(repository, files, archive_path)
        pipeline_jar = pipeline_jar.resolve()
        _verify_pipeline_jar(pipeline_jar)
        jar_path = staging / PIPELINE_JAR
        jar_path.write_bytes(pipeline_jar.read_bytes())
        manifest = {
            "schema": 1,
            "environment": ENVIRONMENT,
            "project": PROJECT,
            "run_id": run_id,
            "commit": commit,
            "images": images,
            "source_archive": SOURCE_ARCHIVE,
            "source_files": source_hashes,
            "pipeline_job": {
                "file": PIPELINE_JAR,
                "jar_sha256": _sha256(jar_path),
                "source_sha256": source_hashes[PIPELINE_SOURCE],
            },
            "ready_for_dev_pipeline": True,
            "started": False,
            "acceptance": "pending",
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        checksums = "\n".join(
            f"{_sha256(staging / name)}  {name}" for name in (SOURCE_ARCHIVE, PIPELINE_JAR, MANIFEST)
        ) + "\n"
        (staging / CHECKSUMS).write_text(checksums, encoding="utf-8")
        with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            for name in (SOURCE_ARCHIVE, PIPELINE_JAR, MANIFEST, CHECKSUMS):
                path = staging / name
                info = bundle.gettarinfo(str(path), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                with path.open("rb") as stream:
                    bundle.addfile(info, stream)
    if output.stat().st_size > MAX_BUNDLE_BYTES:
        output.unlink(missing_ok=True)
        raise ValueError("candidate bundle exceeds size limit")
    return {"bundle": str(output), "sha256": _sha256(output), "bytes": output.stat().st_size, **manifest}


def verify(bundle: Path) -> dict:
    bundle = bundle.resolve()
    if not bundle.is_file() or bundle.is_symlink() or bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("candidate bundle missing or too large")
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
        if names != ALLOWED_TOP_LEVEL or any(not _safe_bundle_member(member) for member in members):
            raise ValueError("candidate bundle contents are not fixed")
        payload = {}
        for name in names:
            stream = archive.extractfile(name)
            if stream is None:
                raise ValueError("candidate bundle entry unreadable")
            payload[name] = stream.read()
    manifest = json.loads(payload[MANIFEST].decode("utf-8"))
    if (manifest.get("schema") != 1 or manifest.get("environment") != ENVIRONMENT
            or manifest.get("project") != PROJECT or manifest.get("ready_for_dev_pipeline") is not True
            or manifest.get("started") is not False or manifest.get("acceptance") != "pending"):
        raise ValueError("candidate manifest identity mismatch")
    _require_commit(manifest.get("commit", ""))
    _require_run_id(manifest.get("run_id", ""))
    images = manifest.get("images", {})
    if set(images) != {"mysql", "redis", "worker", "flink"}:
        raise ValueError("candidate image set incomplete")
    for name, image in images.items():
        _require_digest(image, name)
    source_hashes = manifest.get("source_files")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise ValueError("candidate source manifest missing")
    _verify_archive_bytes(payload[SOURCE_ARCHIVE], source_hashes)
    pipeline_job = manifest.get("pipeline_job")
    if not isinstance(pipeline_job, dict) or pipeline_job.get("file") != PIPELINE_JAR:
        raise ValueError("candidate PipelineJob identity missing")
    if pipeline_job.get("source_sha256") != source_hashes.get(PIPELINE_SOURCE):
        raise ValueError("candidate PipelineJob source hash mismatch")
    if pipeline_job.get("jar_sha256") != hashlib.sha256(payload[PIPELINE_JAR]).hexdigest():
        raise ValueError("candidate PipelineJob JAR hash mismatch")
    _verify_pipeline_jar_bytes(payload[PIPELINE_JAR])
    checksum_lines = payload[CHECKSUMS].decode("utf-8").splitlines()
    expected_checksums = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in checksum_lines if "  " in line}
    if expected_checksums != {SOURCE_ARCHIVE: hashlib.sha256(payload[SOURCE_ARCHIVE]).hexdigest(), PIPELINE_JAR: hashlib.sha256(payload[PIPELINE_JAR]).hexdigest(), MANIFEST: hashlib.sha256(payload[MANIFEST]).hexdigest()}:
        raise ValueError("candidate bundle checksums mismatch")
    return {"verified": True, "sha256": _sha256(bundle), "bytes": bundle.stat().st_size,
            "environment": ENVIRONMENT, "project": PROJECT, "run_id": manifest["run_id"],
            "commit": manifest["commit"], "images": images}


def _safe_bundle_member(member: tarfile.TarInfo) -> bool:
    return member.isfile() and not member.issym() and not member.islnk() and member.name in ALLOWED_TOP_LEVEL


def _verify_archive_bytes(data: bytes, expected_hashes: dict[str, str]) -> None:
    temporary = tempfile.NamedTemporaryFile(delete=False)
    try:
        temporary.write(data)
        temporary.close()
        _verify_archive(Path(temporary.name), expected_hashes)
    finally:
        Path(temporary.name).unlink(missing_ok=True)


def _verify_pipeline_jar_bytes(data: bytes) -> None:
    temporary = tempfile.NamedTemporaryFile(delete=False)
    try:
        temporary.write(data)
        temporary.close()
        _verify_pipeline_jar(Path(temporary.name))
    finally:
        Path(temporary.name).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--repository", type=Path, default=Path("."))
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--commit", required=True)
    build_parser.add_argument("--run-id", required=True)
    build_parser.add_argument("--pipeline-jar", type=Path, required=True)
    for name in ("mysql", "redis", "worker", "flink"):
        build_parser.add_argument(f"--{name}-image", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "build":
            result = build(args.repository, args.output, commit=args.commit, run_id=args.run_id,
                           pipeline_jar=args.pipeline_jar,
                           mysql_image=args.mysql_image, redis_image=args.redis_image,
                           worker_image=args.worker_image, flink_image=args.flink_image)
        else:
            result = verify(args.bundle)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError) as failure:
        # Keep the public error stable while exposing a short, non-sensitive
        # diagnostic for CI.  The candidate contains no credentials, but an
        # exception can still include paths or parser details; never print a
        # full traceback or arbitrary exception text to the workflow log.
        reason = type(failure).__name__
        detail = re.sub(r"(?i)(password|secret|token|cookie|authorization|bearer)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", str(failure))
        print(json.dumps({"error": "candidate_operation_failed", "reason": reason, "detail": detail[:160]}), file=sys.stderr)
        raise SystemExit("Dev pipeline candidate operation failed; preserve evidence") from None


if __name__ == "__main__":
    main()
