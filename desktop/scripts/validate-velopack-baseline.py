#!/usr/bin/env python3
"""Validate that a Velopack baseline belongs to the requested Windows target."""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree


TARGETS = {
    "win7-x64": "com.bhzh.binhu.win7.x64",
    "win10-x64": "com.bhzh.binhu.win10.x64",
}
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read_metadata(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as package:
            names = [name for name in package.namelist() if name.lower().endswith(".nuspec")]
            if len(names) != 1:
                raise ValueError("baseline must contain exactly one nuspec")
            root = ElementTree.fromstring(package.read(names[0]))
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise ValueError(f"invalid baseline package: {error}") from error
    values = {
        element.tag.rsplit("}", 1)[-1]: (element.text or "").strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] in {"id", "version"}
    }
    package_id = values.get("id", "")
    version = values.get("version", "")
    if not package_id or not version:
        raise ValueError("baseline nuspec is missing id or version")
    return package_id, version


def validate(target: str, package: Path, expected_version: str) -> None:
    if target not in TARGETS:
        raise ValueError(f"unsupported target: {target}")
    if not SEMVER_RE.fullmatch(expected_version):
        raise ValueError(f"invalid expected version: {expected_version}")
    if not package.is_file() or package.stat().st_size <= 0:
        raise ValueError(f"baseline package is missing or empty: {package}")
    package_id, version = read_metadata(package)
    expected_id = TARGETS[target]
    if package_id != expected_id:
        raise ValueError(f"baseline PackageId {package_id!r} does not belong to {target}")
    if version != expected_version:
        raise ValueError(f"baseline version {version!r} does not match {expected_version!r}")
    if not package.name.startswith(expected_id + "-") or not package.name.endswith("-full.nupkg"):
        raise ValueError(f"baseline filename does not belong to {target}: {package.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    validate(args.target, args.package, args.expected_version)
    print(f"validated {args.target} baseline: {args.package.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
