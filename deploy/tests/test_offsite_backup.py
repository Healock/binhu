from __future__ import annotations

import gzip
import hashlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.binhu_offsite_backup import (
    DATABASE_MARKERS,
    BackupError,
    ingest_backups,
    push_latest_backup,
    receive_backup,
    validate_backup,
)


def backup_payload(extra: bytes = b"") -> bytes:
    raw = b"\n".join((b"-- MySQL dump", *DATABASE_MARKERS, extra))
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb") as archive:
        archive.write(raw)
    return output.getvalue()


class OffsiteBackupTests(unittest.TestCase):
    def test_receive_and_ingest_valid_backup(self) -> None:
        payload = backup_payload()
        checksum = hashlib.sha256(payload).hexdigest()
        name = "binhu-db-20260807T180000Z-job13.sql.gz"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = receive_backup(
                root / "inbox",
                io.BytesIO(payload),
                f"upload {name} {len(payload)} {checksum}",
            )
            self.assertEqual("stored", result["status"])
            repeated = receive_backup(
                root / "inbox",
                io.BytesIO(payload),
                f"upload {name} {len(payload)} {checksum}",
            )
            self.assertEqual("already_present", repeated["status"])
            ingested = ingest_backups(root / "inbox", root / "archive", 7)
            self.assertEqual([name], ingested["ingested"])
            archived = root / "archive" / name
            self.assertTrue(archived.is_file())
            if os.name != "nt":
                self.assertEqual(0o600, archived.stat().st_mode & 0o777)

    def test_receive_rejects_command_traversal_hash_and_trailing_bytes(self) -> None:
        payload = backup_payload()
        checksum = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory)
            invalid = (
                f"upload ../bad.sql.gz {len(payload)} {checksum}",
                f"upload binhu-db-20260807T180000Z-job1.sql.gz {len(payload)} {'0' * 64}",
                f"upload binhu-db-20260807T180000Z-job1.sql.gz {len(payload)} {checksum}",
            )
            streams = (payload, payload, payload + b"x")
            for command, stream in zip(invalid, streams, strict=True):
                with self.subTest(command=command), self.assertRaises(BackupError):
                    receive_backup(inbox, io.BytesIO(stream), command)

    def test_receive_never_overwrites_same_name_with_different_content(self) -> None:
        original = backup_payload(b"original")
        replacement = backup_payload(b"replacement")
        name = "binhu-db-20260807T180000Z-job1.sql.gz"
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory)
            first_checksum = hashlib.sha256(original).hexdigest()
            receive_backup(
                inbox,
                io.BytesIO(original),
                f"upload {name} {len(original)} {first_checksum}",
            )
            replacement_checksum = hashlib.sha256(replacement).hexdigest()
            with self.assertRaises(BackupError):
                receive_backup(
                    inbox,
                    io.BytesIO(replacement),
                    f"upload {name} {len(replacement)} {replacement_checksum}",
                )
            self.assertEqual(original, (inbox / name).read_bytes())

    def test_validate_rejects_missing_database_and_corrupt_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.sql.gz"
            with gzip.open(missing, "wb") as output:
                output.write(b"USE `OnlineData`;\n")
            corrupt = root / "corrupt.sql.gz"
            corrupt.write_bytes(b"not gzip")
            for path in (missing, corrupt):
                with self.subTest(path=path), self.assertRaises(BackupError):
                    validate_backup(path)

    def test_ingest_retention_keeps_latest_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive"
            archive.mkdir()
            old = archive / "binhu-db-20260701T180000Z-job1.sql.gz"
            newest = archive / "binhu-db-20260702T180000Z-job2.sql.gz"
            old.write_bytes(backup_payload(b"old"))
            newest.write_bytes(backup_payload(b"new"))
            expired = time.time() - 30 * 86400
            os.utime(old, (expired - 10, expired - 10))
            os.utime(newest, (expired, expired))
            result = ingest_backups(root / "inbox", archive, 7)
            self.assertFalse(old.exists())
            self.assertTrue(newest.exists())
            self.assertEqual([old.name], result["removed"])

    def test_push_is_idempotent_and_uses_strict_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            source_dir.mkdir()
            source = source_dir / "binhu-db-20260807T180000Z-job13.sql.gz"
            source.write_bytes(backup_payload())
            identity = root / "id_ed25519"
            known_hosts = root / "known_hosts"
            identity.write_text("private", encoding="utf-8")
            known_hosts.write_text("host key", encoding="utf-8")
            state = root / "state.json"

            completed = type("Completed", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
            with patch("deploy.binhu_offsite_backup.subprocess.run", return_value=completed) as run:
                first = push_latest_backup(
                    source_dir,
                    state,
                    host="10.77.0.1",
                    port=22,
                    user="binhu-backup",
                    identity=identity,
                    known_hosts=known_hosts,
                )
                second = push_latest_backup(
                    source_dir,
                    state,
                    host="10.77.0.1",
                    port=22,
                    user="binhu-backup",
                    identity=identity,
                    known_hosts=known_hosts,
                )
            self.assertEqual("uploaded", first["status"])
            self.assertEqual("already_uploaded", second["status"])
            self.assertEqual(1, run.call_count)
            command = run.call_args.args[0]
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("PasswordAuthentication=no", command)


if __name__ == "__main__":
    unittest.main()
