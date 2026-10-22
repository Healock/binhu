#!/usr/bin/env python3
"""Validate a client release against the currently published platform versions."""

from __future__ import annotations

import re
import sys


def parse_version(value: str) -> tuple[int, int, int] | None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        return None
    return tuple(map(int, value.split(".")))


def validate_versions(incoming: str, win7: str, win10: str, android: str) -> None:
    target = parse_version(incoming)
    if target is None:
        raise ValueError("invalid repository VERSION")
    if bool(win7) != bool(win10) or (win7 and win7 != win10):
        raise ValueError("server Windows platform versions are inconsistent")
    if not win7 and incoming != "0.25.15":
        raise ValueError("an empty server must start at baseline 0.25.15")
    published = [parse_version(value) for value in (win7, android) if value]
    if any(version is None for version in published):
        raise ValueError("server client version is invalid")
    if published and target < max(published):
        raise ValueError("VERSION would downgrade a published client")


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("usage: validate_release_versions.py VERSION WIN7_VERSION WIN10_VERSION ANDROID_VERSION")
    try:
        validate_versions(*sys.argv[1:])
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
