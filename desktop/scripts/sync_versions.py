#!/usr/bin/env python3
"""Synchronize desktop package versions from the repository VERSION file."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
if not re.fullmatch(r"\d+\.\d+\.\d+", VERSION):
    raise SystemExit(f"invalid VERSION: {VERSION}")


def update_json(path: Path, fields: tuple[str, ...]) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    target = data
    for field in fields[:-1]:
        target = target[field]
    if target[fields[-1]] != VERSION:
        target[fields[-1]] = VERSION
        changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def replace(path: Path, pattern: str, replacement: str) -> bool:
    original = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, original, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"version field not found in {path}")
    if updated != original:
        path.write_text(updated, encoding="utf-8")
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = []
    json_targets = (
        (ROOT / "desktop/package.json", ("version",)),
        (ROOT / "desktop/package-lock.json", ("version",)),
        (ROOT / "desktop/package-lock.json", ("packages", "", "version")),
        (ROOT / "desktop/apps/win7-electron/package.json", ("version",)),
        (ROOT / "desktop/apps/win10-tauri/package.json", ("version",)),
        (ROOT / "desktop/apps/win10-tauri/package-lock.json", ("version",)),
        (ROOT / "desktop/apps/win10-tauri/package-lock.json", ("packages", "", "version")),
        (ROOT / "desktop/packages/desktop-contract/package.json", ("version",)),
        (ROOT / "desktop/config/desktop.config.json", ("appVersion",)),
        (ROOT / "desktop/apps/win10-tauri/src-tauri/tauri.conf.json", ("version",)),
    )
    for path, fields in json_targets:
        if update_json(path, fields):
            changed.append(str(path.relative_to(ROOT)))
    text_targets = (
        (ROOT / "desktop/apps/win10-tauri/src-tauri/Cargo.toml", r'^(version\s*=\s*)"[^"]+"', rf'\g<1>"{VERSION}"'),
        (ROOT / "desktop/apps/win10-tauri/src-tauri/Cargo.lock", r'(?ms)(name = "binhu-win10-tauri"\nversion = )"[^"]+"', rf'\g<1>"{VERSION}"'),
        (ROOT / "desktop/apps/win7-electron/src/preload.js", r"(appVersion:\s*)'[^']+'", rf"\g<1>'{VERSION}'"),
        (ROOT / "desktop/apps/win7-vxkex/installer/BinhuWin7VxKex.iss", r'(#define AppVersion\s+)"[^"]+"', rf'\g<1>"{VERSION}"'),
    )
    for path, pattern, replacement in text_targets:
        if replace(path, pattern, replacement):
            changed.append(str(path.relative_to(ROOT)))
    numeric = VERSION + ".0"
    path = ROOT / "desktop/apps/win7-vxkex/installer/BinhuWin7VxKex.iss"
    if replace(path, r'(#define NumericVersion\s+)"[^"]+"', rf'\g<1>"{numeric}"'):
        changed.append(str(path.relative_to(ROOT)))
    if args.check and changed:
        raise SystemExit("desktop versions are not synchronized: " + ", ".join(sorted(set(changed))))
    print(VERSION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
