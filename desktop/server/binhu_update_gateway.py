#!/usr/bin/env python3
"""Restricted SSH gateway for publishing Binhu desktop update feeds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from xml.etree import ElementTree

try:
    import fcntl
except ModuleNotFoundError:  # Windows unit tests do not provide POSIX flock.
    fcntl = None

PLATFORMS = ("win7-x64", "win10-x64")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,199}$")
MAX_BUNDLE_BYTES = 8 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
MAX_FILES = 100


class PublishError(RuntimeError):
    pass


def root_path() -> Path:
    return Path(os.environ.get("BINHU_UPDATE_ROOT", "/srv/binhu-updates"))


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(value)
    if not match:
        raise PublishError(f"invalid SemVer: {value}")
    return tuple(int(part) for part in match.groups())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_exact(stream: BinaryIO, destination: Path, expected_size: int) -> str:
    if expected_size <= 0 or expected_size > MAX_BUNDLE_BYTES:
        raise PublishError("bundle length is outside the allowed range")
    digest = hashlib.sha256()
    remaining = expected_size
    with destination.open("wb") as output:
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise PublishError("upload ended before declared length")
            output.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    if stream.read(1):
        raise PublishError("upload contains bytes beyond declared length")
    return digest.hexdigest()


def safe_member_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise PublishError(f"unsafe archive path: {name}")
    if path == PurePosixPath("release.json"):
        return path
    if len(path.parts) != 2 or path.parts[0] not in PLATFORMS:
        raise PublishError(f"unexpected archive path: {name}")
    if not FILE_RE.fullmatch(path.name):
        raise PublishError(f"invalid release file name: {path.name}")
    if path.name != "releases.stable.json" and not path.name.endswith((".nupkg", ".exe", ".sha256", ".json")):
        raise PublishError(f"file type is not allowed: {path.name}")
    return path


def extract_bundle(bundle: Path, destination: Path) -> None:
    count = 0
    total = 0
    with tarfile.open(bundle, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise PublishError(f"archive links and special files are forbidden: {member.name}")
            safe_member_name(member.name)
            count += 1
            total += member.size
            if count > MAX_FILES or member.size > MAX_FILE_BYTES or total > MAX_BUNDLE_BYTES:
                raise PublishError("archive exceeds file count or size limits")
            source = archive.extractfile(member)
            if source is None:
                raise PublishError(f"unable to read archive member: {member.name}")
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PublishError(f"invalid JSON file {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise PublishError(f"JSON root must be an object: {path.name}")
    return value


def nupkg_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as package:
            nuspecs = [name for name in package.namelist() if name.lower().endswith(".nuspec")]
            if len(nuspecs) != 1:
                raise PublishError(f"nupkg must contain one nuspec: {path.name}")
            root = ElementTree.fromstring(package.read(nuspecs[0]))
    except (zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise PublishError(f"invalid nupkg {path.name}: {error}") from error
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "version" and element.text:
            return element.text.strip()
    raise PublishError(f"nupkg has no version: {path.name}")


def validate_platform(stage: Path, platform: str, expected_version: str, declared: list[dict]) -> dict:
    platform_root = stage / platform
    if not platform_root.is_dir():
        raise PublishError(f"missing platform directory: {platform}")
    declared_by_name: dict[str, dict] = {}
    for item in declared:
        if not isinstance(item, dict):
            raise PublishError(f"invalid file declaration for {platform}")
        name = item.get("name")
        if not isinstance(name, str) or not FILE_RE.fullmatch(name) or name in declared_by_name:
            raise PublishError(f"invalid or duplicate file declaration: {name}")
        if name != "releases.stable.json" and not name.endswith((".nupkg", ".exe", ".sha256", ".json")):
            raise PublishError(f"declared file type is not allowed: {name}")
        declared_by_name[name] = item

    actual_names = {path.name for path in platform_root.iterdir() if path.is_file()}
    if actual_names != set(declared_by_name):
        raise PublishError(f"declared files do not match archive for {platform}")
    if "releases.stable.json" not in actual_names:
        raise PublishError(f"missing Velopack feed for {platform}")

    for name, item in declared_by_name.items():
        path = platform_root / name
        size = item.get("size")
        digest = item.get("sha256")
        if not isinstance(size, int) or size < 0 or size != path.stat().st_size:
            raise PublishError(f"size mismatch: {platform}/{name}")
        if not isinstance(digest, str) or not HASH_RE.fullmatch(digest) or sha256_file(path) != digest:
            raise PublishError(f"SHA-256 mismatch: {platform}/{name}")
        if name.endswith(".nupkg"):
            package_version = nupkg_version(path)
            if parse_version(package_version) > parse_version(expected_version):
                raise PublishError(f"package version is newer than the release: {platform}/{name}")

    feed = load_json(platform_root / "releases.stable.json")
    assets = feed.get("Assets")
    if not isinstance(assets, list) or not assets:
        raise PublishError(f"empty or invalid Velopack feed for {platform}")
    full_packages = []
    current_delta_packages = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise PublishError(f"invalid Velopack asset for {platform}")
        filename = asset.get("FileName")
        if not isinstance(filename, str) or filename not in actual_names or not filename.endswith(".nupkg"):
            raise PublishError(f"feed references missing package: {platform}/{filename}")
        asset_version = asset.get("Version")
        if not isinstance(asset_version, str) or parse_version(asset_version) > parse_version(expected_version):
            raise PublishError(f"feed version mismatch: {platform}/{filename}")
        if str(asset.get("Type", "")).lower() == "full" and asset_version == expected_version:
            full_packages.append(filename)
        if str(asset.get("Type", "")).lower() == "delta" and asset_version == expected_version:
            current_delta_packages.append(filename)
    if len(full_packages) != 1:
        raise PublishError(f"feed must declare exactly one full package for {platform}")
    if expected_version == "0.25.15" and current_delta_packages:
        raise PublishError(f"baseline release must not contain delta packages for {platform}")
    if expected_version != "0.25.15" and not current_delta_packages:
        raise PublishError(f"release must contain a delta package for {platform}")
    return {"fullPackage": full_packages[0], "files": sorted(actual_names)}


def state_file(root: Path, platform: str) -> Path:
    return root / "state" / f"{platform}.json"


def read_state(root: Path, platform: str) -> dict:
    path = state_file(root, platform)
    if not path.exists():
        return {"version": "", "commit": "", "fullPackage": ""}
    return load_json(path)


def status(root: Path) -> None:
    print("STATUS=ok")
    for platform in PLATFORMS:
        current = read_state(root, platform)
        prefix = platform.replace("-", "_").upper()
        print(f"{prefix}_VERSION={current.get('version', '')}")
        print(f"{prefix}_COMMIT={current.get('commit', '')}")
        print(f"{prefix}_FULL_PACKAGE={current.get('fullPackage', '')}")


def archive_old_releases(root: Path, platform: str) -> None:
    history_root = root / "state" / "history" / platform
    entries = []
    for path in history_root.glob("*.json"):
        try:
            entries.append((parse_version(path.stem), path, load_json(path)))
        except PublishError:
            continue
    entries.sort(reverse=True)
    retained_names = {
        name
        for _, _, metadata in entries[:5]
        for name in metadata.get("files", [])
    }
    public_root = root / "public" / platform
    archive_root = root / "archive" / platform
    archive_root.mkdir(parents=True, exist_ok=True)
    for _, metadata_path, metadata in entries[5:]:
        version_root = archive_root / metadata_path.stem
        version_root.mkdir(parents=True, exist_ok=True)
        for name in metadata.get("files", []):
            source = public_root / name
            if source.exists() and name != "releases.stable.json" and name not in retained_names:
                os.replace(source, version_root / name)
        os.replace(metadata_path, version_root / "release.json")


def publish(root: Path, bundle: Path, expected_version: str, expected_commit: str) -> None:
    parse_version(expected_version)
    if not COMMIT_RE.fullmatch(expected_commit):
        raise PublishError("invalid commit id")
    current_states = {platform: read_state(root, platform) for platform in PLATFORMS}
    populated = [state for state in current_states.values() if state.get("version")]
    if populated and len(populated) != len(PLATFORMS):
        raise PublishError("server platform state is incomplete")
    if populated:
        versions = {state.get("version") for state in populated}
        commits = {state.get("commit") for state in populated}
        if len(versions) != 1 or len(commits) != 1:
            raise PublishError("server platform state is inconsistent")
    elif expected_version != "0.25.15":
        raise PublishError("an empty server must start at baseline 0.25.15")
    with tempfile.TemporaryDirectory(prefix="binhu-update-stage-", dir=root / "incoming") as temporary:
        stage = Path(temporary)
        extract_bundle(bundle, stage)
        manifest = load_json(stage / "release.json")
        if manifest.get("schemaVersion") != 1 or manifest.get("version") != expected_version:
            raise PublishError("release manifest version or schema mismatch")
        if manifest.get("commit") != expected_commit or manifest.get("signed") is not False:
            raise PublishError("release manifest commit or signature status mismatch")
        platforms = manifest.get("platforms")
        if not isinstance(platforms, dict) or set(platforms) != set(PLATFORMS):
            raise PublishError("release manifest must contain both Windows platforms")

        validated = {}
        for platform in PLATFORMS:
            current = current_states[platform]
            if current.get("version"):
                current_version = parse_version(str(current["version"]))
                incoming_version = parse_version(expected_version)
                if incoming_version < current_version:
                    raise PublishError(f"downgrade rejected for {platform}")
                if incoming_version == current_version:
                    if current.get("commit") != expected_commit:
                        raise PublishError(f"same version has a different commit for {platform}")
                    raise PublishError(f"version is already published for {platform}")
            entry = platforms[platform]
            if not isinstance(entry, dict) or not isinstance(entry.get("files"), list):
                raise PublishError(f"invalid platform manifest for {platform}")
            validated[platform] = validate_platform(stage, platform, expected_version, entry["files"])

        for platform in PLATFORMS:
            public_root = root / "public" / platform
            public_root.mkdir(parents=True, exist_ok=True)
            source_root = stage / platform
            for name in validated[platform]["files"]:
                if name == "releases.stable.json":
                    continue
                temporary_target = public_root / f".{name}.new"
                shutil.copy2(source_root / name, temporary_target)
                os.replace(temporary_target, public_root / name)

        for platform in PLATFORMS:
            public_root = root / "public" / platform
            feed_temporary = public_root / ".releases.stable.json.new"
            shutil.copy2(stage / platform / "releases.stable.json", feed_temporary)
            os.replace(feed_temporary, public_root / "releases.stable.json")
            metadata = {
                "version": expected_version,
                "commit": expected_commit,
                "fullPackage": validated[platform]["fullPackage"],
                "files": validated[platform]["files"],
                "signed": False,
            }
            state_file(root, platform).parent.mkdir(parents=True, exist_ok=True)
            state_temp = state_file(root, platform).with_suffix(".json.new")
            state_temp.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(state_temp, state_file(root, platform))
            history = root / "state" / "history" / platform / f"{expected_version}.json"
            history.parent.mkdir(parents=True, exist_ok=True)
            history.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            archive_old_releases(root, platform)


def main(argv: list[str] | None = None, stdin: BinaryIO | None = None) -> int:
    root = root_path()
    for path in (root / "incoming", root / "public", root / "archive", root / "state"):
        path.mkdir(parents=True, exist_ok=True)
    command = (os.environ.get("SSH_ORIGINAL_COMMAND") or " ".join(argv or sys.argv[1:])).strip()
    parts = command.split()
    try:
        if parts == ["status"]:
            status(root)
            return 0
        if len(parts) != 5 or parts[0] != "publish":
            raise PublishError("only status and publish are allowed")
        version, commit, size_text, expected_hash = parts[1:]
        if not size_text.isdigit() or not HASH_RE.fullmatch(expected_hash):
            raise PublishError("invalid publish metadata")
        bundle = root / "incoming" / f"{version}-{commit}.tar.gz"
        actual_hash = read_exact(stdin or sys.stdin.buffer, bundle, int(size_text))
        if actual_hash != expected_hash:
            bundle.unlink(missing_ok=True)
            raise PublishError("bundle SHA-256 mismatch")
        lock_path = root / "publish.lock"
        with lock_path.open("a+b") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                publish(root, bundle, version, commit)
            finally:
                bundle.unlink(missing_ok=True)
        print(f"PUBLISHED_VERSION={version}")
        print(f"PUBLISHED_COMMIT={commit}")
        return 0
    except (OSError, tarfile.TarError, PublishError) as error:
        print(f"ERROR={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
