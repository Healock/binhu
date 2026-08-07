#!/usr/bin/env python3
"""Restricted off-site transfer for Binhu three-database backups.

The receiver is intended to be used as an SSH forced command.  It accepts one
validated backup stream and cannot list, read, replace, or delete archived
files.  A root-owned ingest timer moves accepted files out of the writable
inbox and applies the seven-day retention policy.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path


BACKUP_NAME = re.compile(r"binhu-db-\d{8}T\d{6}Z-job\d+\.sql\.gz")
SHA256 = re.compile(r"[0-9a-f]{64}")
SSH_HOST = re.compile(r"[A-Za-z0-9.-]+")
SSH_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
DATABASE_MARKERS = (
    b"USE `OnlineData`;",
    b"USE `OnlineDataArchive`;",
    b"USE `daily_report`;",
)
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024


class BackupError(RuntimeError):
    """A safe, user-facing backup validation error."""


def _safe_backup_name(value: str) -> str:
    if not BACKUP_NAME.fullmatch(value):
        raise BackupError("invalid backup filename")
    return value


def _safe_child(root: Path, name: str) -> Path:
    root = root.resolve()
    candidate = (root / _safe_backup_name(name)).resolve()
    if candidate.parent != root:
        raise BackupError("backup path escaped its configured directory")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_backup(
    path: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> tuple[int, str]:
    if not path.is_file() or path.is_symlink():
        raise BackupError("backup is not a regular file")
    size = path.stat().st_size
    if size <= 0 or size > max_bytes:
        raise BackupError("backup size is outside the allowed range")
    if expected_size is not None and size != expected_size:
        raise BackupError("backup size does not match the upload request")

    digest = _sha256(path)
    if expected_sha256 is not None:
        expected_sha256 = expected_sha256.lower()
        if not SHA256.fullmatch(expected_sha256) or digest != expected_sha256:
            raise BackupError("backup SHA-256 does not match")

    found = {marker: False for marker in DATABASE_MARKERS}
    carry = b""
    total = 0
    try:
        with gzip.open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > max_uncompressed_bytes:
                    raise BackupError("backup expands beyond the allowed limit")
                searchable = carry + chunk
                for marker in found:
                    if marker in searchable:
                        found[marker] = True
                carry = searchable[-128:]
    except (OSError, EOFError) as exc:
        raise BackupError("backup gzip validation failed") from exc
    if not all(found.values()):
        raise BackupError("backup does not contain all three database markers")
    return size, digest


def _parse_upload_command(command_text: str) -> tuple[str, int, str]:
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        raise BackupError("invalid upload command") from exc
    if len(parts) != 4 or parts[0] != "upload":
        raise BackupError("only the fixed backup upload command is allowed")
    name = _safe_backup_name(parts[1])
    try:
        size = int(parts[2])
    except ValueError as exc:
        raise BackupError("invalid upload size") from exc
    checksum = parts[3].lower()
    if size <= 0 or size > DEFAULT_MAX_BYTES or not SHA256.fullmatch(checksum):
        raise BackupError("invalid upload metadata")
    return name, size, checksum


def receive_backup(inbox: Path, stream, command_text: str) -> dict:
    name, expected_size, expected_checksum = _parse_upload_command(command_text)
    inbox = inbox.resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    target = _safe_child(inbox, name)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{name}.", suffix=".partial", dir=inbox
    )
    temporary = Path(temporary_name)
    received = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as output:
            while received < expected_size:
                chunk = stream.read(min(1024 * 1024, expected_size - received))
                if not chunk:
                    raise BackupError("backup stream ended early")
                output.write(chunk)
                digest.update(chunk)
                received += len(chunk)
            if stream.read(1):
                raise BackupError("backup stream contains trailing data")
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_checksum:
            raise BackupError("backup stream SHA-256 does not match")
        size, checksum = validate_backup(
            temporary,
            expected_size=expected_size,
            expected_sha256=expected_checksum,
        )
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing_size, existing_checksum = validate_backup(
                target,
                expected_size=expected_size,
                expected_sha256=expected_checksum,
            )
            return {
                "status": "already_present",
                "filename": name,
                "size": existing_size,
                "sha256": existing_checksum,
            }
        return {"status": "stored", "filename": name, "size": size, "sha256": checksum}
    finally:
        temporary.unlink(missing_ok=True)


def ingest_backups(inbox: Path, archive: Path, retention_days: int) -> dict:
    if retention_days < 1 or retention_days > 365:
        raise BackupError("retention days must be between 1 and 365")
    inbox = inbox.resolve()
    archive = archive.resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    os.chmod(archive, 0o700)

    ingested: list[str] = []
    for source in sorted(inbox.iterdir()):
        if not source.is_file() or source.is_symlink() or not BACKUP_NAME.fullmatch(source.name):
            continue
        _, checksum = validate_backup(source)
        target = _safe_child(archive, source.name)
        if target.exists():
            _, archived_checksum = validate_backup(target)
            if checksum != archived_checksum:
                raise BackupError(f"archive conflict for {source.name}")
            os.chmod(target, 0o600)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(target, 0, 0)
            source.unlink()
            continue
        os.replace(source, target)
        os.chmod(target, 0o600)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(target, 0, 0)
        ingested.append(target.name)

    archived = sorted(
        (
            item
            for item in archive.iterdir()
            if item.is_file() and not item.is_symlink() and BACKUP_NAME.fullmatch(item.name)
        ),
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )
    protected = archived[0] if archived else None
    cutoff = time.time() - retention_days * 86400
    removed: list[str] = []
    for item in archived:
        if item == protected or item.stat().st_mtime >= cutoff:
            continue
        item.unlink()
        removed.append(item.name)
    return {"status": "ok", "ingested": ingested, "removed": removed}


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".state.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def push_latest_backup(
    source_dir: Path,
    state_path: Path,
    *,
    host: str,
    port: int,
    user: str,
    identity: Path,
    known_hosts: Path,
) -> dict:
    if not SSH_HOST.fullmatch(host) or not SSH_USER.fullmatch(user):
        raise BackupError("invalid SSH destination")
    if port < 1 or port > 65535:
        raise BackupError("invalid SSH port")
    if not identity.is_file() or not known_hosts.is_file():
        raise BackupError("SSH identity or known_hosts file is missing")
    if not source_dir.is_dir():
        raise BackupError("platform backup directory is missing")
    candidates = sorted(
        (
            item
            for item in source_dir.resolve().iterdir()
            if item.is_file() and not item.is_symlink() and BACKUP_NAME.fullmatch(item.name)
        ),
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )
    if not candidates:
        return {"status": "no_backup"}
    source = candidates[0]
    size, checksum = validate_backup(source)
    state = _load_state(state_path)
    if state.get("filename") == source.name and state.get("sha256") == checksum:
        return {"status": "already_uploaded", "filename": source.name}

    command = [
        "ssh",
        "-F",
        "/dev/null",
        "-i",
        str(identity),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "ConnectTimeout=15",
        "-p",
        str(port),
        f"{user}@{host}",
        f"upload {source.name} {size} {checksum}",
    ]
    try:
        with source.open("rb") as payload:
            completed = subprocess.run(
                command,
                stdin=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=1800,
                check=False,
                text=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("off-site backup SSH transfer failed") from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")[-1000:]
        raise BackupError(message or "off-site backup upload failed")
    _write_state(
        state_path,
        {
            "filename": source.name,
            "sha256": checksum,
            "size": size,
            "uploaded_at": int(time.time()),
        },
    )
    return {"status": "uploaded", "filename": source.name, "size": size, "sha256": checksum}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    receive = subparsers.add_parser("receive")
    receive.add_argument("--inbox", type=Path, required=True)

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--inbox", type=Path, required=True)
    ingest.add_argument("--archive", type=Path, required=True)
    ingest.add_argument("--retention-days", type=int, default=7)

    push = subparsers.add_parser("push")
    push.add_argument("--source-dir", type=Path, required=True)
    push.add_argument("--state", type=Path, required=True)
    push.add_argument("--host", required=True)
    push.add_argument("--port", type=int, default=22)
    push.add_argument("--user", default="binhu-backup")
    push.add_argument("--identity", type=Path, required=True)
    push.add_argument("--known-hosts", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "receive":
            result = receive_backup(
                args.inbox,
                sys.stdin.buffer,
                os.environ.get("SSH_ORIGINAL_COMMAND", ""),
            )
        elif args.action == "ingest":
            result = ingest_backups(args.inbox, args.archive, args.retention_days)
        else:
            result = push_latest_backup(
                args.source_dir,
                args.state,
                host=args.host,
                port=args.port,
                user=args.user,
                identity=args.identity,
                known_hosts=args.known_hosts,
            )
    except BackupError as exc:
        print(f"off-site backup rejected: {exc}", file=sys.stderr)
        return 64
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
