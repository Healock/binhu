"""Build and verify an immutable Staging event-pipeline candidate bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile
import tempfile
import zipfile

from .staging_compose import IMAGE_KEYS, project_for


RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
DEV_RUN_RE = re.compile(r"^dev-[0-9]{8}-[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_RE = re.compile(r"^staging-[0-9a-f]{16}$")
ALLOWED = frozenset({"source.tar.gz", "pipeline-job.jar", "manifest.json", "SHA256SUMS"})
SOURCE_PREFIXES = ("deploy/environments/event_pipeline/", "load-tests/staging/")
SOURCE_FILES = frozenset({"load-tests/requirements.txt"})
MAX_BYTES = 128 * 1024 * 1024


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(repository: Path) -> list[Path]:
    result: list[Path] = []
    for root_name in ("deploy/environments/event_pipeline", "load-tests/staging"):
        root = repository / root_name
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Staging candidate source missing")
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(repository)
            if any(part in {"target", "__pycache__"} for part in relative.parts) or path.suffix == ".pyc":
                continue
            result.append(path)
    result.append(repository / "load-tests" / "requirements.txt")
    if any(not path.is_file() or path.is_symlink() for path in result):
        raise ValueError("Staging load-test dependency manifest missing")
    return sorted(set(result))


def _safe_source_name(name: str) -> bool:
    path = Path(name)
    return (not path.is_absolute() and ".." not in path.parts
            and (name in SOURCE_FILES or name.startswith(SOURCE_PREFIXES)))


def _write_source(repository: Path, paths: list[Path], target: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    with tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in paths:
            name = path.relative_to(repository).as_posix()
            info = archive.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            with path.open("rb") as stream:
                archive.addfile(info, stream)
            hashes[name] = _sha(path)
    return hashes


def _verify_source(data: bytes, expected: dict[str, str]) -> None:
    with tempfile.NamedTemporaryFile(delete=False) as stream:
        stream.write(data)
        name = Path(stream.name)
    try:
        actual: dict[str, str] = {}
        with tarfile.open(name, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile() or member.issym() or member.islnk() or not _safe_source_name(member.name):
                    raise ValueError("unsafe Staging source archive")
                content = archive.extractfile(member)
                if content is None:
                    raise ValueError("unreadable Staging source archive")
                actual[member.name] = hashlib.sha256(content.read()).hexdigest()
        if actual != expected:
            raise ValueError("Staging source archive hash mismatch")
    finally:
        name.unlink(missing_ok=True)


def _verify_jar(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("compiled PipelineJob JAR missing")
    try:
        with zipfile.ZipFile(path) as archive:
            if "PipelineJob.class" not in archive.namelist():
                raise ValueError("PipelineJob class missing")
    except zipfile.BadZipFile as exc:
        raise ValueError("compiled PipelineJob JAR invalid") from exc


def _identity(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value or ""):
        raise ValueError(f"invalid {label}")
    return value


def build(repository: Path, output: Path, *, run_id: str, commit: str, images: dict[str, str],
          pipeline_jar: Path, candidate_image_digest: str, configuration_sha256: str,
          staging_snapshot_id: str, dev_acceptance_run_id: str) -> dict:
    repository = repository.resolve()
    _identity(run_id, RUN_RE, "Staging run id")
    _identity(commit, COMMIT_RE, "commit")
    _identity(candidate_image_digest, DIGEST_RE, "application digest")
    _identity(configuration_sha256, SHA_RE, "configuration digest")
    _identity(staging_snapshot_id, SNAPSHOT_RE, "Staging snapshot")
    _identity(dev_acceptance_run_id, DEV_RUN_RE, "Dev acceptance run id")
    if set(images) != IMAGE_KEYS or any(not DIGEST_RE.fullmatch(value or "") for value in images.values()):
        raise ValueError("six immutable event-pipeline images required")
    _verify_jar(pipeline_jar)
    paths = _files(repository)
    output = output.resolve()
    if output.exists():
        raise ValueError("Staging candidate output must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="staging-pipeline-candidate-") as temporary:
        root = Path(temporary)
        source = root / "source.tar.gz"
        hashes = _write_source(repository, paths, source)
        jar = root / "pipeline-job.jar"
        jar.write_bytes(pipeline_jar.read_bytes())
        manifest = {
            "schema": 1, "environment": "staging", "run_id": run_id,
            "project": project_for(run_id), "commit": commit, "images": images,
            "candidate_image_digest": candidate_image_digest,
            "configuration_sha256": configuration_sha256,
            "staging_snapshot_id": staging_snapshot_id,
            "dev_acceptance": {
                "status": "passed", "run_id": dev_acceptance_run_id,
                "candidate_image_digest": candidate_image_digest,
                "event_pipeline_image_digests": images,
            },
            "event_pipeline_image_digests": images,
            "source_files": hashes,
            "pipeline_job": {
                "jar_sha256": _sha(jar),
                "source_sha256": hashes["deploy/environments/event_pipeline/PipelineJob.java"],
            },
            "fictional_only": True, "production_data": False,
            "ready_for_staging": True, "started": False, "acceptance": "pending",
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        checksums = "\n".join(f"{_sha(root / name)}  {name}" for name in ("source.tar.gz", "pipeline-job.jar", "manifest.json")) + "\n"
        (root / "SHA256SUMS").write_text(checksums, encoding="utf-8")
        with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for name in sorted(ALLOWED):
                path = root / name
                info = archive.gettarinfo(str(path), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
    if output.stat().st_size > MAX_BYTES:
        output.unlink(missing_ok=True)
        raise ValueError("Staging candidate exceeds size limit")
    return {**manifest, "bundle": str(output), "bundle_sha256": _sha(output), "bytes": output.stat().st_size}


def verify(bundle: Path) -> dict:
    if bundle.is_symlink() or not bundle.is_file() or bundle.stat().st_size > MAX_BYTES:
        raise ValueError("Staging candidate missing or too large")
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        if {item.name for item in members} != ALLOWED or any(not item.isfile() or item.issym() or item.islnk() for item in members):
            raise ValueError("Staging candidate contents are not fixed")
        payload = {item.name: archive.extractfile(item).read() for item in members}
    manifest = json.loads(payload["manifest.json"])
    if (manifest.get("schema") != 1 or manifest.get("environment") != "staging"
            or manifest.get("ready_for_staging") is not True or manifest.get("production_data") is not False
            or manifest.get("fictional_only") is not True or manifest.get("project") != project_for(manifest.get("run_id", ""))):
        raise ValueError("Staging candidate identity mismatch")
    _identity(manifest["commit"], COMMIT_RE, "commit")
    _identity(manifest["candidate_image_digest"], DIGEST_RE, "application digest")
    _identity(manifest["configuration_sha256"], SHA_RE, "configuration digest")
    _identity(manifest["staging_snapshot_id"], SNAPSHOT_RE, "Staging snapshot")
    images = manifest.get("images", {})
    if set(images) != IMAGE_KEYS or manifest.get("event_pipeline_image_digests") != images:
        raise ValueError("Staging event-pipeline image set mismatch")
    if manifest.get("dev_acceptance", {}).get("event_pipeline_image_digests") != images:
        raise ValueError("Dev and Staging event-pipeline images differ")
    _verify_source(payload["source.tar.gz"], manifest["source_files"])
    if hashlib.sha256(payload["pipeline-job.jar"]).hexdigest() != manifest["pipeline_job"]["jar_sha256"]:
        raise ValueError("Staging PipelineJob JAR hash mismatch")
    expected = {name: hashlib.sha256(payload[name]).hexdigest() for name in ("source.tar.gz", "pipeline-job.jar", "manifest.json")}
    actual = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in payload["SHA256SUMS"].decode().splitlines()}
    if actual != expected:
        raise ValueError("Staging candidate checksum mismatch")
    return {"verified": True, "run_id": manifest["run_id"], "commit": manifest["commit"],
            "bundle_sha256": _sha(bundle), "images": images}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--repository", type=Path, default=Path("."))
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--run-id", required=True)
    build_parser.add_argument("--commit", required=True)
    build_parser.add_argument("--pipeline-jar", type=Path, required=True)
    build_parser.add_argument("--candidate-image-digest", required=True)
    build_parser.add_argument("--configuration-sha256", required=True)
    build_parser.add_argument("--staging-snapshot-id", required=True)
    build_parser.add_argument("--dev-acceptance-run-id", required=True)
    for name in sorted(IMAGE_KEYS):
        build_parser.add_argument(f"--{name.replace('_', '-')}-image", required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "verify":
            result = verify(args.bundle)
        else:
            images = {name: getattr(args, name + "_image") for name in IMAGE_KEYS}
            result = build(
                args.repository, args.output, run_id=args.run_id, commit=args.commit,
                images=images, pipeline_jar=args.pipeline_jar,
                candidate_image_digest=args.candidate_image_digest,
                configuration_sha256=args.configuration_sha256,
                staging_snapshot_id=args.staging_snapshot_id,
                dev_acceptance_run_id=args.dev_acceptance_run_id,
            )
        print(json.dumps(result, sort_keys=True))
    except Exception:
        raise SystemExit("Staging candidate operation failed; preserve private evidence") from None


if __name__ == "__main__":
    main()
