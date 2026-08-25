#!/usr/bin/env python3
"""Validate and stage one signed Binhu arm64 APK update release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_NAME = "com.bhzh.binhu.android"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def version_code(version: str) -> int:
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise SystemExit(f"invalid Android release version: {version}")
    major, minor, patch = (int(part) for part in match.groups())
    if minor >= 1000 or patch >= 1000:
        raise SystemExit("Android minor and patch versions must be below 1000")
    return major * 1_000_000 + minor * 1_000 + patch


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_tool(tool: Path, *arguments: str) -> str:
    command = [str(tool), *arguments]
    if tool.suffix.lower() in {".bat", ".cmd"}:
        command = ["cmd.exe", "/d", "/c", str(tool), *arguments]
    result = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        raise SystemExit(f"{tool.name} rejected the APK:\n{output.strip()}")
    return output


def extract_sha256_digest(output: str) -> str:
    """Extract one certificate SHA-256 digest from apksigner output.

    Android build-tools have emitted both contiguous and separator-delimited
    fingerprints, and the surrounding label can vary between releases.  Only
    consider lines that explicitly identify a certificate SHA-256 value, then
    require exactly one normalized 64-character digest so the public-key
    fingerprint or an unrelated checksum cannot be used as the APK signer.
    """
    digests: set[str] = set()
    for line in output.splitlines():
        if not re.search(r"certificate.*sha\s*[- ]?\s*256", line, flags=re.IGNORECASE):
            continue
        for candidate in re.findall(r"(?<![0-9a-f])(?:[0-9a-f]{2}[\s:-]*){32}(?![0-9a-f])", line, flags=re.IGNORECASE):
            normalized = normalize_digest(candidate)
            if HASH_RE.fullmatch(normalized):
                digests.add(normalized)
    if len(digests) != 1:
        raise SystemExit("apksigner did not return exactly one SHA-256 signing certificate digest")
    return digests.pop()


def inspect_apk(apk: Path, aapt2: Path, apksigner: Path) -> tuple[str, int, str, str]:
    badging = run_tool(aapt2, "dump", "badging", str(apk))
    package = re.search(
        r"^package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'",
        badging,
        flags=re.MULTILINE,
    )
    if not package:
        raise SystemExit("aapt2 did not return Android package version metadata")
    signing = run_tool(apksigner, "verify", "--verbose", "--print-certs", str(apk))
    return package.group(1), int(package.group(2)), package.group(3), extract_sha256_digest(signing)


def normalize_digest(value: str) -> str:
    return "".join(character.lower() for character in value if character.lower() in "0123456789abcdef")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--minimum-version", required=True)
    parser.add_argument("--expected-signer", required=True)
    parser.add_argument("--aapt2", type=Path, required=True)
    parser.add_argument("--apksigner", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not args.apk.is_file():
        raise SystemExit(f"APK does not exist: {args.apk}")
    if not args.aapt2.is_file() or not args.apksigner.is_file():
        raise SystemExit("aapt2 and apksigner are required")
    if not COMMIT_RE.fullmatch(args.commit):
        raise SystemExit("commit must be a 40-character lowercase Git SHA")
    expected_code = version_code(args.version)
    minimum_code = version_code(args.minimum_version)
    if minimum_code > expected_code:
        raise SystemExit("minimum Android version cannot exceed the published version")
    expected_signer = normalize_digest(args.expected_signer)
    if not HASH_RE.fullmatch(expected_signer):
        raise SystemExit("expected signer must be a SHA-256 certificate fingerprint")

    package_name, actual_code, actual_version, actual_signer = inspect_apk(
        args.apk,
        args.aapt2,
        args.apksigner,
    )
    if package_name != PACKAGE_NAME:
        raise SystemExit(f"unexpected Android package: {package_name}")
    if actual_version != args.version or actual_code != expected_code:
        raise SystemExit(
            f"APK version mismatch: expected {args.version}/{expected_code}, "
            f"got {actual_version}/{actual_code}"
        )
    if actual_signer != expected_signer:
        raise SystemExit("APK signing certificate does not match ANDROID_SIGNING_CERT_SHA256")

    args.output.mkdir(parents=True, exist_ok=True)
    filename = f"Binhu-Android-arm64-{args.version}.apk"
    staged_apk = args.output / filename
    shutil.copy2(args.apk, staged_apk)
    manifest = {
        "schemaVersion": 1,
        "channel": "stable",
        "version": args.version,
        "versionCode": expected_code,
        "commit": args.commit,
        "publishedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "apk": {
            "filename": filename,
            "size": staged_apk.stat().st_size,
            "sha256": sha256(staged_apk),
            "signerSha256": actual_signer,
        },
    }
    manifest_path = args.output / "manifest.stable.json"
    policy_path = args.output / "policy.stable.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    policy_path.write_text(
        json.dumps({"minimumVersion": args.minimum_version}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    checksums = args.output / "checksums.sha256"
    checksum_files = (staged_apk, manifest_path, policy_path)
    checksums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in checksum_files),
        encoding="ascii",
    )
    print(staged_apk)
    print(manifest_path)
    print(policy_path)
    print(checksums)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
