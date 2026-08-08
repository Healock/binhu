import ast
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

import bcrypt
from fastapi import HTTPException

from deps import require_super_admin
from ops_agent import _container_name, decode_docker_stream
from routers import admin_ops
from routers.admin_ops import _require_log_source, router as admin_ops_router
from services import backups
from services.audit_display import (
    ACTION_LABELS,
    TARGET_TYPE_LABELS,
    action_label,
    actor_name,
    detail_items,
    target_display,
)
from services.ops_database import require_database_name
from services.ops_redaction import redact_text, sanitize_detail, sanitized_json


class FakeCursor:
    def __init__(self, now: datetime):
        self.now = now
        self.next_run_at = now - timedelta(seconds=1)
        self.last_sql = ""
        self.executed = []
        self.lastrowid = 91
        self.insert_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.last_sql = normalized
        self.executed.append((normalized, params))
        if normalized.startswith("INSERT INTO _backup_jobs"):
            self.insert_count += 1
        if normalized.startswith(
            "UPDATE _backup_schedule SET last_triggered_at"
        ):
            self.next_run_at = params[0]

    async def fetchone(self):
        if self.last_sql.startswith("SELECT GET_LOCK"):
            return (1,)
        if self.last_sql.startswith("SELECT RELEASE_LOCK"):
            return (1,)
        if self.last_sql.startswith(
            "SELECT enabled, run_hour, run_minute, next_run_at"
        ):
            return (1, 2, 0, self.next_run_at)
        if self.last_sql == "SELECT UTC_TIMESTAMP()":
            return (self.now,)
        if self.last_sql.startswith("SELECT id FROM _backup_jobs"):
            return None
        if self.last_sql.startswith(
            "SELECT config_value FROM _system_config"
        ):
            return ("Asia/Shanghai",)
        return None


class FakePool:
    def __init__(self, cursor):
        self.cursor = cursor
        self.connection = MagicMock()
        self.connection.cursor.return_value = cursor

    async def acquire(self):
        return self.connection

    def release(self, conn):
        return None


class CleanupCursor(FakeCursor):
    def __init__(self):
        super().__init__(datetime(2026, 7, 28, 18, 0))

    async def fetchall(self):
        if "ORDER BY finished_at DESC" in self.last_sql:
            return [
                (2, "binhu-db-20260728T020000Z-job2.sql.gz"),
                (1, "binhu-db-20260720T020000Z-job1.sql.gz"),
            ]
        if "finished_at < DATE_SUB" in self.last_sql:
            return [(1, "binhu-db-20260720T020000Z-job1.sql.gz")]
        return []


class AdminOperationsSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_every_operations_route_requires_super_admin(self):
        self.assertTrue(admin_ops_router.routes)
        for route in admin_ops_router.routes:
            self.assertTrue(
                any(
                    dependency.call is require_super_admin
                    for dependency in route.dependant.dependencies
                ),
                route.path,
            )

    async def test_non_super_admin_roles_are_rejected(self):
        for role in ("admin", "leader", "member"):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException) as raised:
                    await require_super_admin({"id": 1, "role": role})
                self.assertEqual(raised.exception.status_code, 403)

    def test_all_external_names_use_fixed_allowlists(self):
        self.assertEqual(_require_log_source("backend"), "backend")
        self.assertEqual(_container_name("mysql"), "binhu-mysql")
        self.assertEqual(require_database_name("OnlineData"), "OnlineData")
        for invalid in (
            "../backend",
            "other",
            "OnlineData; DROP DATABASE OnlineData",
        ):
            with self.subTest(invalid=invalid):
                if invalid.startswith("OnlineData"):
                    with self.assertRaises(ValueError):
                        require_database_name(invalid)
                else:
                    with self.assertRaises(HTTPException):
                        _require_log_source(invalid)

    def test_redaction_removes_common_credentials(self):
        raw = (
            "Authorization: Bearer top-secret "
            "password=hunter2 token=abc123 "
            "Cookie: binhu_session=session-value "
            "?access_token=oauth-value&next=1\n"
            '{"password":"json-secret","refresh_token":"json-token",'
            '"authorization":"Bearer json-bearer"}'
        )
        redacted = redact_text(raw)
        for secret in (
            "top-secret",
            "hunter2",
            "abc123",
            "session-value",
            "oauth-value",
            "json-secret",
            "json-token",
            "json-bearer",
        ):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 5)

    def test_nested_diagnostic_and_audit_details_are_sanitized(self):
        value = {
            "status": "ok",
            "password": "secret-password",
            "nested": {
                "refresh_token": "secret-token",
                "message": "Authorization=Bearer another-secret",
            },
        }
        sanitized = sanitize_detail(value)
        serialized = sanitized_json(value)
        self.assertEqual(sanitized["password"], "[REDACTED]")
        self.assertEqual(
            sanitized["nested"]["refresh_token"],
            "[REDACTED]",
        )
        self.assertNotIn("secret-password", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("another-secret", serialized)

    def test_docker_multiplexed_logs_decode_without_binary_headers(self):
        stderr = b"2026-07-28T01:02:03Z error line\n"
        stdout = b"2026-07-28T01:02:04Z normal line\n"
        payload = (
            bytes([2, 0, 0, 0])
            + len(stderr).to_bytes(4, "big")
            + stderr
            + bytes([1, 0, 0, 0])
            + len(stdout).to_bytes(4, "big")
            + stdout
        )
        self.assertEqual(
            decode_docker_stream(payload),
            [
                {
                    "stream": "stderr",
                    "message": "2026-07-28T01:02:03Z error line",
                },
                {
                    "stream": "stdout",
                    "message": "2026-07-28T01:02:04Z normal line",
                },
            ],
        )


class AuditDisplayTests(unittest.IsolatedAsyncioTestCase):
    def test_all_current_literal_audit_codes_have_display_labels(self):
        actions: set[str] = set()
        target_types: set[str] = set()
        router_directory = Path(__file__).resolve().parents[1] / "routers"
        for path in router_directory.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if function_name != "record_admin_audit":
                    continue
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    actions.add(str(node.args[1].value))
                for keyword in node.keywords:
                    if keyword.arg == "target_type" and isinstance(
                        keyword.value, ast.Constant
                    ):
                        target_types.add(str(keyword.value.value))

        self.assertEqual(actions - ACTION_LABELS.keys(), set())
        self.assertEqual(target_types - TARGET_TYPE_LABELS.keys(), set())

    def test_current_member_name_is_preferred_over_identifier_username(self):
        self.assertEqual(
            actor_name(
                member_name="示例人员",
                display_name="显示姓名",
                current_username="operator_007",
                recorded_username="operator_007",
                user_id=7,
            ),
            "示例人员",
        )
        self.assertEqual(
            actor_name(
                member_name=None,
                display_name=None,
                current_username=None,
                recorded_username="",
                user_id=None,
            ),
            "系统自动任务",
        )

    def test_codes_and_detail_fields_have_human_readable_labels(self):
        self.assertEqual(action_label("police_dispatch.import"), "导入下发数据")
        self.assertEqual(action_label("police_dispatch.delete"), "删除下发批次")
        self.assertEqual(
            target_display("police_dispatch_batch", "1"),
            "数据下发批次 · #1",
        )
        self.assertEqual(
            detail_items(
                {"accepted": 74, "conflicts": 0, "import_kind": "community"}
            ),
            [
                {"key": "accepted", "label": "导入数量", "value": "74"},
                {"key": "conflicts", "label": "冲突数量", "value": "0"},
                {"key": "import_kind", "label": "导入类型", "value": "居民小区"},
            ],
        )

    async def test_audit_api_keeps_raw_fields_and_adds_display_fields(self):
        class AuditCursor:
            def __init__(self):
                self.last_sql = ""
                self.executed = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def execute(self, sql, params=None):
                self.last_sql = " ".join(sql.split())
                self.executed.append((self.last_sql, params))

            async def fetchone(self):
                return (1,)

            async def fetchall(self):
                return [
                    (
                        9,
                        7,
                        "operator_007",
                        "police_dispatch.import",
                        "police_dispatch_batch",
                        "1",
                        "success",
                        '{"row_count":528}',
                        "127.0.0.1",
                        "browser",
                        datetime(2026, 8, 5, 7, 42, 21),
                        "示例人员",
                        "示例人员",
                        "operator_007",
                    )
                ]

        cursor = AuditCursor()
        pool = FakePool(cursor)
        with patch.object(admin_ops.db_manager, "get_pool", return_value=pool):
            result = await admin_ops.audit_log(
                page=1,
                page_size=50,
                action="",
                user={"id": 1, "role": "super_admin"},
            )

        event = result["data"][0]
        self.assertEqual(event["actor_name"], "示例人员")
        self.assertEqual(event["actor_account"], "operator_007")
        self.assertEqual(event["action_label"], "导入下发数据")
        self.assertEqual(event["target_display"], "数据下发批次 · #1")
        self.assertEqual(event["result_label"], "成功")
        self.assertEqual(event["detail"], {"row_count": 528})
        self.assertEqual(
            event["detail_items"],
            [{"key": "row_count", "label": "数据行数", "value": "528"}],
        )
        self.assertTrue(
            any(
                "LEFT JOIN _grid_members" in sql
                for sql, _ in cursor.executed
            )
        )
        self.assertTrue(
            any(
                "LEFT JOIN _sync_log AS sync_task" in sql
                and "COALESCE(sync_task.status, audit.result)" in sql
                for sql, _ in cursor.executed
            )
        )
        self.assertTrue(
            any(
                option["value"] == "police_dispatch.import"
                for option in result["action_options"]
            )
        )

    async def test_sync_audit_displays_live_sync_task_status(self):
        class SyncAuditCursor:
            def __init__(self):
                self.last_sql = ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def execute(self, sql, params=None):
                self.last_sql = " ".join(sql.split())

            async def fetchone(self):
                return (1,)

            async def fetchall(self):
                return [(
                    10, 1, "admin", "sync.trigger", "sync", "2608",
                    "success", None, "127.0.0.1", "browser",
                    datetime(2026, 8, 5, 8, 0), "管理员", None, "admin",
                )]

        cursor = SyncAuditCursor()
        with patch.object(
            admin_ops.db_manager,
            "get_pool",
            return_value=FakePool(cursor),
        ):
            result = await admin_ops.audit_log(
                page=1,
                page_size=50,
                action="sync.trigger",
                user={"id": 1, "role": "super_admin"},
            )

        self.assertEqual(result["data"][0]["result"], "success")
        self.assertEqual(result["data"][0]["result_label"], "成功")
        self.assertIn("sync_task.id=CAST", cursor.last_sql)


class BackupTests(unittest.IsolatedAsyncioTestCase):
    def test_daily_schedule_uses_business_timezone(self):
        now = datetime(2026, 7, 28, 17, 30, tzinfo=timezone.utc)
        next_run = backups.calculate_next_run_utc(
            now,
            "Asia/Shanghai",
            2,
            0,
        )
        self.assertEqual(next_run, datetime(2026, 7, 28, 18, 0))

    async def test_due_schedule_is_claimed_only_once(self):
        cursor = FakeCursor(datetime(2026, 7, 28, 18, 0))
        pool = FakePool(cursor)
        with patch.object(
            backups.db_manager,
            "get_pool",
            return_value=pool,
        ):
            first = await backups.claim_due_backup_task()
            second = await backups.claim_due_backup_task()

        self.assertEqual(first, 91)
        self.assertIsNone(second)
        self.assertEqual(cursor.insert_count, 1)

    def test_backup_file_is_atomic_gzip_and_hashes_download_file(self):
        sql = b"-- backup\n" + b"".join(
            (
                f"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{database}`;\n"
                f"-- Current Database: `{database}`\n"
                f"USE `{database}`;\n"
                "CREATE TABLE `sample` (`id` int);\n"
            ).encode("utf-8")
            for database in backups.BACKUP_DATABASES
        )

        def fake_run(command, **kwargs):
            self.assertNotIn("shell", kwargs)
            self.assertIn("--single-transaction", command)
            self.assertIn("OnlineDataArchive", command)
            self.assertIn("daily_report", command)
            self.assertIn("PlatformData", command)
            self.assertIn("WorkflowData", command)
            kwargs["stdout"].write(sql)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            backups.settings,
            "BACKUP_DIR",
            directory,
        ), patch.object(
            backups.shutil,
            "which",
            return_value="/usr/bin/mysqldump",
        ), patch.object(
            backups.subprocess,
            "run",
            side_effect=fake_run,
        ):
            filename, size_bytes, checksum = backups._create_backup_file(12)
            path = Path(directory) / filename

            self.assertTrue(path.is_file())
            self.assertEqual(size_bytes, path.stat().st_size)
            self.assertEqual(
                checksum,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            with gzip.open(path, "rb") as source:
                self.assertEqual(source.read(), sql)
            manifest = json.loads((path.parent / f"{filename}.manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([item["database"] for item in manifest["databases"]], list(backups.BACKUP_DATABASES))
            self.assertTrue(all(item["included"] for item in manifest["databases"]))
            self.assertEqual(
                [item for item in Path(directory).iterdir() if item.name.startswith(".")],
                [],
            )

    def test_download_path_rejects_unknown_and_traversal_names(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            backups.settings,
            "BACKUP_DIR",
            directory,
        ):
            valid = "binhu-db-20260728T020000Z-job9.sql.gz"
            self.assertEqual(
                backups._safe_platform_path(valid),
                Path(directory).resolve() / valid,
            )
            for invalid in (
                "../" + valid,
                "old-backup.sql.gz",
                "binhu-db-20260728T020000Z-job9.sql",
            ):
                self.assertIsNone(backups._safe_platform_path(invalid))

    async def test_cleanup_removes_only_expired_platform_backup(self):
        cursor = CleanupCursor()
        pool = FakePool(cursor)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            backups.settings,
            "BACKUP_DIR",
            directory,
        ), patch.object(
            backups.db_manager,
            "get_pool",
            return_value=pool,
        ):
            newest = Path(directory) / (
                "binhu-db-20260728T020000Z-job2.sql.gz"
            )
            expired = Path(directory) / (
                "binhu-db-20260720T020000Z-job1.sql.gz"
            )
            legacy = Path(directory) / "historical-backup.sql.gz"
            newest.write_bytes(b"new")
            expired.write_bytes(b"old")
            legacy.write_bytes(b"legacy")

            removed = await backups.cleanup_expired_backups()

            self.assertEqual(removed, 1)
            self.assertTrue(newest.exists())
            self.assertFalse(expired.exists())
            self.assertTrue(legacy.exists())
            self.assertTrue(
                any(
                    sql.startswith(
                        "UPDATE _backup_jobs SET status='expired'"
                    )
                    for sql, _ in cursor.executed
                )
            )
            self.assertTrue(
                any(
                    sql.startswith("DELETE FROM _admin_audit_log")
                    for sql, _ in cursor.executed
                )
            )

    async def test_failed_backup_notifies_super_admins(self):
        cursor = FakeCursor(datetime(2026, 7, 28, 18, 0))
        pool = FakePool(cursor)
        notify = AsyncMock()
        with patch.object(
            backups.db_manager,
            "get_pool",
            return_value=pool,
        ), patch.object(
            backups,
            "_create_backup_file",
            side_effect=RuntimeError("backup failed password=hidden-value"),
        ), patch.object(
            backups,
            "create_backup_failure_notifications",
            new=notify,
        ):
            await backups.run_backup_task(14)

        notify.assert_awaited_once()
        self.assertNotIn("hidden-value", notify.await_args.args[1])

    async def test_download_password_uses_current_account_hash(self):
        password_hash = bcrypt.hashpw(
            b"correct-password",
            bcrypt.gensalt(),
        ).decode()
        cursor = FakeCursor(datetime(2026, 7, 28, 18, 0))

        async def password_row():
            return (password_hash,)

        cursor.fetchone = password_row
        pool = FakePool(cursor)
        with patch.object(
            admin_ops.db_manager,
            "get_pool",
            return_value=pool,
        ):
            self.assertTrue(
                await admin_ops._verify_current_password(
                    7,
                    "correct-password",
                )
            )
            self.assertFalse(
                await admin_ops._verify_current_password(
                    7,
                    "wrong-password",
                )
            )


if __name__ == "__main__":
    unittest.main()
