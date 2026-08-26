#!/usr/bin/env python3
"""Restricted SSH gateway for publishing Binhu desktop update feeds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
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

WINDOWS_PLATFORMS = ("win7-x64", "win10-x64")
ANDROID_PLATFORM = "android-arm64"
PLATFORMS = (*WINDOWS_PLATFORMS, ANDROID_PLATFORM)
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
    allowed_suffixes = (".apk", ".sha256", ".json") if path.parts[0] == ANDROID_PLATFORM else (
        ".nupkg", ".exe", ".sha256", ".json"
    )
    if not path.name.endswith(allowed_suffixes):
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


def nupkg_metadata(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as package:
            nuspecs = [name for name in package.namelist() if name.lower().endswith(".nuspec")]
            if len(nuspecs) != 1:
                raise PublishError(f"nupkg must contain one nuspec: {path.name}")
            root = ElementTree.fromstring(package.read(nuspecs[0]))
    except (zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise PublishError(f"invalid nupkg {path.name}: {error}") from error
    metadata = {
        element.tag.rsplit("}", 1)[-1]: element.text.strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] in {"id", "version"} and element.text
    }
    package_id = metadata.get("id")
    version = metadata.get("version")
    if not package_id or not version:
        raise PublishError(f"nupkg has no package id or version: {path.name}")
    return package_id, version


def nupkg_version(path: Path) -> str:
    return nupkg_metadata(path)[1]


def validate_declared_files(
    stage: Path,
    platform: str,
    declared: list[dict],
    allowed_suffixes: tuple[str, ...],
) -> tuple[Path, dict[str, dict], set[str]]:
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
        if not name.endswith(allowed_suffixes):
            raise PublishError(f"declared file type is not allowed: {name}")
        declared_by_name[name] = item

    actual_names = {path.name for path in platform_root.iterdir() if path.is_file()}
    if actual_names != set(declared_by_name):
        raise PublishError(f"declared files do not match archive for {platform}")
    for name, item in declared_by_name.items():
        path = platform_root / name
        size = item.get("size")
        digest = item.get("sha256")
        if not isinstance(size, int) or size < 0 or size != path.stat().st_size:
            raise PublishError(f"size mismatch: {platform}/{name}")
        if not isinstance(digest, str) or not HASH_RE.fullmatch(digest) or sha256_file(path) != digest:
            raise PublishError(f"SHA-256 mismatch: {platform}/{name}")
    return platform_root, declared_by_name, actual_names


def validate_windows_platform(stage: Path, platform: str, expected_version: str, declared: list[dict]) -> dict:
    platform_root, _, actual_names = validate_declared_files(
        stage,
        platform,
        declared,
        (".nupkg", ".exe", ".sha256", ".json"),
    )
    if "releases.stable.json" not in actual_names:
        raise PublishError(f"missing Velopack feed for {platform}")
    expected_package_id = f"com.bhzh.binhu.{platform}"
    for name in actual_names:
        if name.endswith(".nupkg"):
            package_id, package_version = nupkg_metadata(platform_root / name)
            if package_id != expected_package_id:
                raise PublishError(f"cross-platform package in {platform}: {name}")
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
        if asset.get("PackageId") != expected_package_id:
            raise PublishError(f"feed contains cross-platform package: {platform}/{filename}")
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


def android_version_code(version: str) -> int:
    major, minor, patch = parse_version(version)
    if minor >= 1000 or patch >= 1000:
        raise PublishError("Android minor and patch versions must be below 1000")
    return major * 1_000_000 + minor * 1_000 + patch


def find_android_tool(environment_name: str, executable: str) -> str:
    configured = os.environ.get(environment_name, "").strip()
    if configured:
        if Path(configured).is_file():
            return configured
        raise PublishError(f"configured Android verifier does not exist: {environment_name}")
    discovered = shutil.which(executable)
    if discovered:
        return discovered
    candidates = sorted(Path("/opt/android-sdk/build-tools").glob(f"*/{executable}"), reverse=True)
    if candidates:
        return str(candidates[0])
    raise PublishError(f"Android publishing requires {executable}")


def extract_android_signer_digest(output: str) -> str:
    digests: set[str] = set()
    for line in output.splitlines():
        if not re.search(r"certificate.*sha\s*[- ]?\s*256", line, flags=re.IGNORECASE):
            continue
        for candidate in re.findall(
            r"(?<![0-9a-f])(?:[0-9a-f]{2}[\s:-]*){32}(?![0-9a-f])",
            line,
            flags=re.IGNORECASE,
        ):
            normalized = "".join(
                character.lower()
                for character in candidate
                if character.lower() in "0123456789abcdef"
            )
            if len(normalized) == 64:
                digests.add(normalized)
    if len(digests) != 1:
        raise PublishError("Android signing certificate SHA-256 digest is unavailable or ambiguous")
    return digests.pop()


def inspect_android_apk(path: Path) -> dict:
    aapt2 = find_android_tool("BINHU_AAPT2", "aapt2")
    apksigner = find_android_tool("BINHU_APKSIGNER", "apksigner")
    try:
        badging = subprocess.run(
            [aapt2, "dump", "badging", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        signing = subprocess.run(
            [apksigner, "verify", "--verbose", "--print-certs", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise PublishError(f"Android APK inspection failed: {error}") from error
    package = re.search(
        r"^package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'",
        badging,
        flags=re.MULTILINE,
    )
    if not package:
        raise PublishError("Android APK metadata or signing certificate is unavailable")
    signer_digest = extract_android_signer_digest(signing)
    return {
        "package": package.group(1),
        "versionCode": int(package.group(2)),
        "version": package.group(3),
        "signerSha256": signer_digest,
    }


def validate_android_platform(stage: Path, expected_version: str, expected_commit: str, declared: list[dict]) -> dict:
    platform_root, _, actual_names = validate_declared_files(
        stage,
        ANDROID_PLATFORM,
        declared,
        (".apk", ".sha256", ".json"),
    )
    required = {"manifest.stable.json", "policy.stable.json", "checksums.sha256"}
    if not required.issubset(actual_names):
        raise PublishError("Android release is missing its manifest, policy or checksums")
    apks = sorted(name for name in actual_names if name.endswith(".apk"))
    if len(apks) != 1:
        raise PublishError("Android release must contain exactly one APK")
    manifest = load_json(platform_root / "manifest.stable.json")
    apk = manifest.get("apk")
    if (
        manifest.get("schemaVersion") != 1
        or manifest.get("channel") != "stable"
        or manifest.get("version") != expected_version
        or manifest.get("versionCode") != android_version_code(expected_version)
        or manifest.get("commit") != expected_commit
        or not isinstance(manifest.get("publishedAt"), str)
        or not manifest.get("publishedAt")
        or not isinstance(apk, dict)
    ):
        raise PublishError("Android stable manifest metadata is invalid")
    apk_name = apks[0]
    apk_path = platform_root / apk_name
    expected_name = f"Binhu-Android-arm64-{expected_version}.apk"
    signer = str(apk.get("signerSha256", "")).lower().replace(":", "")
    if (
        apk_name != expected_name
        or apk.get("filename") != apk_name
        or apk.get("size") != apk_path.stat().st_size
        or apk.get("sha256") != sha256_file(apk_path)
        or not HASH_RE.fullmatch(signer)
    ):
        raise PublishError("Android APK declaration does not match the uploaded file")
    policy = load_json(platform_root / "policy.stable.json")
    minimum = policy.get("minimumVersion")
    if not isinstance(minimum, str) or parse_version(minimum) > parse_version(expected_version):
        raise PublishError("Android minimum version policy is invalid")
    inspection = inspect_android_apk(apk_path)
    if (
        inspection.get("package") != "com.bhzh.binhu.android"
        or inspection.get("version") != expected_version
        or inspection.get("versionCode") != android_version_code(expected_version)
        or inspection.get("signerSha256") != signer
    ):
        raise PublishError("Android APK package, version or signer verification failed")
    checksum_lines = (platform_root / "checksums.sha256").read_text(encoding="ascii").splitlines()
    expected_checksums = {
        f"{sha256_file(platform_root / name)}  {name}"
        for name in (apk_name, "manifest.stable.json", "policy.stable.json")
    }
    if set(checksum_lines) != expected_checksums:
        raise PublishError("Android checksums.sha256 is incomplete or inconsistent")
    return {"apk": apk_name, "files": sorted(actual_names), "signerSha256": signer}


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
        print(f"{prefix}_APK={current.get('apk', '')}")


def fetch_full_package(root: Path, platform: str, filename: str, output: BinaryIO) -> None:
    """Stream only the currently published full package for one platform."""
    if platform not in WINDOWS_PLATFORMS:
        raise PublishError("invalid fetch platform")
    if not FILE_RE.fullmatch(filename) or not filename.endswith("-full.nupkg"):
        raise PublishError("only a valid full package may be fetched")
    current = read_state(root, platform)
    if not current.get("version") or current.get("fullPackage") != filename:
        raise PublishError("requested package is not the current full package")
    package = root / "public" / platform / filename
    if not package.is_file():
        raise PublishError("current full package is unavailable")
    with package.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            output.write(chunk)
    output.flush()


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
            if source.exists() and name not in {"releases.stable.json", "manifest.stable.json"} and name not in retained_names:
                os.replace(source, version_root / name)
        os.replace(metadata_path, version_root / "release.json")


def publish(root: Path, bundle: Path, expected_version: str, expected_commit: str) -> bool:
    parse_version(expected_version)
    if not COMMIT_RE.fullmatch(expected_commit):
        raise PublishError("invalid commit id")
    current_states = {platform: read_state(root, platform) for platform in PLATFORMS}
    windows_populated = [current_states[platform] for platform in WINDOWS_PLATFORMS if current_states[platform].get("version")]
    if windows_populated and len(windows_populated) != len(WINDOWS_PLATFORMS):
        raise PublishError("server Windows platform state is incomplete")
    populated = [state for state in current_states.values() if state.get("version")]
    if windows_populated:
        versions = {state.get("version") for state in windows_populated}
        commits = {state.get("commit") for state in windows_populated}
        if len(versions) != 1 or len(commits) != 1:
            raise PublishError("server Windows platform state is inconsistent")
        android_state = current_states[ANDROID_PLATFORM]
        if android_state.get("version") and (
            android_state.get("version") not in versions or android_state.get("commit") not in commits
        ):
            raise PublishError("server Android platform state is inconsistent")
    elif expected_version != "0.25.15":
        raise PublishError("an empty server must start at baseline 0.25.15")
    with tempfile.TemporaryDirectory(prefix="binhu-update-stage-", dir=root / "incoming") as temporary:
        stage = Path(temporary)
        extract_bundle(bundle, stage)
        manifest = load_json(stage / "release.json")
        if manifest.get("schemaVersion") != 2 or manifest.get("version") != expected_version:
            raise PublishError("release manifest version or schema mismatch")
        if manifest.get("commit") != expected_commit:
            raise PublishError("release manifest commit mismatch")
        platforms = manifest.get("platforms")
        if not isinstance(platforms, dict) or set(platforms) != set(PLATFORMS):
            raise PublishError("release manifest must contain Windows and Android platforms")

        validated = {}
        already_published = True
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
                else:
                    already_published = False
            else:
                already_published = False
            entry = platforms[platform]
            if not isinstance(entry, dict) or not isinstance(entry.get("files"), list):
                raise PublishError(f"invalid platform manifest for {platform}")
            if platform == ANDROID_PLATFORM:
                if entry.get("signed") is not True:
                    raise PublishError("Android release must be signed")
                validated[platform] = validate_android_platform(
                    stage,
                    expected_version,
                    expected_commit,
                    entry["files"],
                )
            else:
                if entry.get("signed") is not False:
                    raise PublishError(f"unsigned Windows release expected for {platform}")
                validated[platform] = validate_windows_platform(stage, platform, expected_version, entry["files"])

        if already_published:
            return False

        for platform in PLATFORMS:
            public_root = root / "public" / platform
            public_root.mkdir(parents=True, exist_ok=True)
            source_root = stage / platform
            pointer_name = "manifest.stable.json" if platform == ANDROID_PLATFORM else "releases.stable.json"
            for name in validated[platform]["files"]:
                if name == pointer_name:
                    continue
                temporary_target = public_root / f".{name}.new"
                shutil.copy2(source_root / name, temporary_target)
                os.replace(temporary_target, public_root / name)

        for platform in PLATFORMS:
            public_root = root / "public" / platform
            pointer_name = "manifest.stable.json" if platform == ANDROID_PLATFORM else "releases.stable.json"
            feed_temporary = public_root / f".{pointer_name}.new"
            shutil.copy2(stage / platform / pointer_name, feed_temporary)
            os.replace(feed_temporary, public_root / pointer_name)
            metadata = {
                "version": expected_version,
                "commit": expected_commit,
                "fullPackage": validated[platform].get("fullPackage", ""),
                "apk": validated[platform].get("apk", ""),
                "files": validated[platform]["files"],
                "signed": platform == ANDROID_PLATFORM,
            }
            state_file(root, platform).parent.mkdir(parents=True, exist_ok=True)
            state_temp = state_file(root, platform).with_suffix(".json.new")
            state_temp.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(state_temp, state_file(root, platform))
            history = root / "state" / "history" / platform / f"{expected_version}.json"
            history.parent.mkdir(parents=True, exist_ok=True)
            history.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            archive_old_releases(root, platform)
    return True


def main(
    argv: list[str] | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    root = root_path()
    for path in (root / "incoming", root / "public", root / "archive", root / "state"):
        path.mkdir(parents=True, exist_ok=True)
    command = (os.environ.get("SSH_ORIGINAL_COMMAND") or " ".join(argv or sys.argv[1:])).strip()
    parts = command.split()
    try:
        if parts == ["status"]:
            status(root)
            return 0
        if len(parts) == 3 and parts[0] == "fetch":
            fetch_full_package(root, parts[1], parts[2], stdout or sys.stdout.buffer)
            return 0
        if len(parts) != 5 or parts[0] != "publish":
            raise PublishError("only status, fetch and publish are allowed")
        version, commit, size_text, expected_hash = parts[1:]
        if not size_text.isdigit() or not HASH_RE.fullmatch(expected_hash):
            raise PublishError("invalid publish metadata")
        bundle = root / "incoming" / f"{version}-{commit}.tar.gz"
        actual_hash = read_exact(stdin or sys.stdin.buffer, bundle, int(size_text))
        if actual_hash != expected_hash:
            bundle.unlink(missing_ok=True)
            raise PublishError("bundle SHA-256 mismatch")
        # Keep the lock in the publisher-owned state directory. The update
        # root itself is intentionally not writable by the restricted account.
        lock_path = root / "state" / "publish.lock"
        with lock_path.open("a+b") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                published = publish(root, bundle, version, commit)
            finally:
                bundle.unlink(missing_ok=True)
        print(f"PUBLISH_STATUS={'published' if published else 'already-published'}")
        print(f"PUBLISHED_VERSION={version}")
        print(f"PUBLISHED_COMMIT={commit}")
        return 0
    except (OSError, tarfile.TarError, PublishError) as error:
        print(f"ERROR={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
