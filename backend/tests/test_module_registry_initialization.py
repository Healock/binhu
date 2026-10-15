"""Execute initializer and PATCH DML against SQLite, without a MySQL service.

The adapter strips MySQL DDL/locking syntax and uses BEGIN IMMEDIATE to
serialize SQLite writers. This verifies persistent data and rollback behavior,
not MySQL InnoDB row locks, driver behavior, or production migration readiness.
"""

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.function_modules import ModulePayload, update_function_module
from services.module_registry import can_access_definition, ensure_function_module_schema


class SqliteCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.connection.calls.append(sql)
        if sql.startswith("CREATE TABLE"):
            # The fixture supplies equivalent portable tables; no claim about DDL.
            return
        if sql.startswith("SHOW COLUMNS"):
            self.result = await asyncio.to_thread(
                self.connection.raw.execute,
                "SELECT name FROM pragma_table_info('_function_modules') WHERE name='owner_unit'",
            )
            return
        if sql.startswith("SHOW INDEX"):
            self.result = await asyncio.to_thread(
                self.connection.raw.execute,
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_function_module_owner'",
            )
            return
        if sql == "ALTER TABLE _function_modules DROP INDEX idx_function_module_owner":
            sql = "DROP INDEX idx_function_module_owner"
        if sql == "START TRANSACTION":
            sql = "BEGIN IMMEDIATE"
        if self.connection.fail_on and self.connection.fail_on in sql:
            raise RuntimeError("synthetic transaction failure")
        sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE").replace(" FOR UPDATE", "")
        sql = sql.replace(
            "ON DUPLICATE KEY UPDATE config_key=config_key",
            "ON CONFLICT(config_key) DO UPDATE SET config_key=excluded.config_key",
        )
        sql = sql.replace("%s", "?")
        self.result = await asyncio.to_thread(self.connection.raw.execute, sql, params or ())

    async def executemany(self, sql, rows):
        for row in rows:
            await self.execute(sql, row)

    async def fetchone(self):
        return await asyncio.to_thread(self.result.fetchone)

    async def fetchall(self):
        return await asyncio.to_thread(self.result.fetchall)


class SqliteConnection:
    def __init__(self, path):
        self.raw = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5)
        self.calls = []
        self.fail_on = None

    def cursor(self):
        return SqliteCursor(self)

    async def begin(self):
        await self.cursor().execute("START TRANSACTION")

    async def commit(self):
        await self.cursor().execute("COMMIT")

    async def rollback(self):
        await self.cursor().execute("ROLLBACK")


class ModuleInitializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "modules.sqlite")
        self.conn = SqliteConnection(self.path)
        self.conn.raw.executescript("""
            CREATE TABLE _system_config(config_key TEXT PRIMARY KEY,config_value TEXT);
            CREATE TABLE _departments(id INTEGER PRIMARY KEY,name TEXT,is_active INTEGER);
            INSERT INTO _departments VALUES(10,'社区警务队',1);
            CREATE TABLE _function_modules(
                module_id TEXT PRIMARY KEY,name TEXT,is_public INTEGER,required_permissions TEXT,
                route TEXT,mobile_visible INTEGER,enabled INTEGER,version INTEGER);
            CREATE TABLE _function_module_units(
                module_id TEXT,department_id INTEGER,granted_by INTEGER,
                PRIMARY KEY(module_id,department_id));
            CREATE TABLE _function_module_versions(
                module_id TEXT,version INTEGER,snapshot TEXT,changed_by INTEGER,
                UNIQUE(module_id,version));
        """)

    def tearDown(self):
        self.conn.raw.close()
        self.directory.cleanup()

    async def initialize(self, conn=None):
        await ensure_function_module_schema((conn or self.conn).cursor())

    def state(self):
        return {
            table: self.conn.raw.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in ("_system_config", "_function_modules", "_function_module_units", "_function_module_versions")
        }

    async def revoke(self, *, enabled=False):
        await update_function_module(
            "community_police", ModulePayload(name="Synthetic module", department_ids=[], enabled=enabled),
            user={"id": 1}, conn=self.conn,
        )

    async def test_first_boot_seeds_bindings_and_repeat_boot_is_identical(self):
        await self.initialize()
        before = self.state()
        self.assertEqual(before["_function_module_units"], [
            ("community_police", 10, None), ("workflow", 10, None),
        ])
        self.assertEqual(before["_system_config"], [("function_module_units_seeded", "1")])
        for module_id, version, snapshot, _ in before["_function_module_versions"]:
            self.assertEqual(json.loads(snapshot)["department_ids"], [10] if module_id in {"community_police", "workflow"} else [])
            self.assertEqual(version, 1)
        await self.initialize()
        self.assertEqual(self.state(), before)

    async def test_patch_revoke_then_restart_preserves_removal_disabled_and_snapshot(self):
        await self.initialize()
        await self.revoke()
        before = self.state()
        await self.initialize()
        self.assertEqual(self.state(), before)
        snapshot = json.loads(self.conn.raw.execute(
            "SELECT snapshot FROM _function_module_versions WHERE module_id='community_police' AND version=2"
        ).fetchone()[0])
        self.assertEqual(snapshot["department_ids"], [])
        self.assertFalse(can_access_definition({"role": "super_admin"}, snapshot))
        await self.revoke(enabled=True)
        before = self.state()
        await self.initialize()
        self.assertEqual(self.state(), before)
        self.assertFalse(can_access_definition(
            {"role": "member", "departments": [{"id": 10}]},
            {"enabled": True, "is_public": False, "department_ids": []},
        ))

    async def test_old_database_without_marker_preserves_admin_revocation(self):
        await self.initialize()
        await self.revoke()
        self.conn.raw.execute("DELETE FROM _system_config")
        before = self.state()
        await self.initialize()
        before["_system_config"] = [("function_module_units_seeded", "1")]
        self.assertEqual(self.state(), before)

    async def test_old_modern_version_one_empty_bindings_are_not_reseeded(self):
        await self.initialize()
        self.conn.raw.execute("DELETE FROM _system_config")
        self.conn.raw.execute("DELETE FROM _function_module_units")
        await self.initialize()
        self.assertEqual(self.state()["_function_module_units"], [])

    async def test_legacy_owner_unit_preserves_history_and_actual_current_configuration(self):
        self.conn.raw.execute("ALTER TABLE _function_modules ADD COLUMN owner_unit TEXT")
        self.conn.raw.execute("CREATE INDEX idx_function_module_owner ON _function_modules(owner_unit)")
        self.conn.raw.execute(
            "INSERT INTO _function_modules VALUES('community_police','Custom module',0,'[]','/custom',0,0,3,'community_police')"
        )
        old_snapshot = json.dumps({"owner_unit": "community_police", "version": 3})
        self.conn.raw.execute("INSERT INTO _function_module_versions VALUES(?,?,?,?)", ("community_police", 3, old_snapshot, 1))
        await self.initialize()
        rows = self.conn.raw.execute(
            "SELECT version,snapshot FROM _function_module_versions WHERE module_id='community_police' ORDER BY version"
        ).fetchall()
        self.assertEqual(rows[0], (3, old_snapshot))
        self.assertEqual(len(rows), 2)
        snapshot = json.loads(rows[1][1])
        self.assertEqual(snapshot["department_ids"], [10])
        self.assertEqual(snapshot["route"], "/custom")
        self.assertFalse(snapshot["enabled"])
        self.assertNotIn("owner_unit", snapshot)
        before = self.state()
        await self.initialize()
        self.assertEqual(self.state(), before)

    async def test_failure_rolls_back_marker_modules_units_and_versions_then_can_retry(self):
        for failure in ("INSERT IGNORE INTO _function_module_units", "INSERT INTO _function_module_versions", "COMMIT"):
            with self.subTest(failure=failure):
                self.conn.fail_on = failure
                with self.assertRaisesRegex(RuntimeError, "synthetic"):
                    await self.initialize()
                self.assertTrue(all(not rows for rows in self.state().values()))
                self.assertIn("ROLLBACK", self.conn.calls)
        self.conn.fail_on = None
        await self.initialize()
        self.assertEqual(len(self.state()["_function_module_units"]), 2)

    async def test_missing_default_department_rolls_back_without_marking_seed_complete(self):
        self.conn.raw.execute("UPDATE _departments SET is_active=0")
        with self.assertRaisesRegex(RuntimeError, "PR-A0"):
            await self.initialize()
        self.assertTrue(all(not rows for rows in self.state().values()))
        self.conn.raw.execute("UPDATE _departments SET is_active=1")
        await self.initialize()
        self.assertEqual(len(self.state()["_function_module_units"]), 2)

    async def test_two_initializers_commit_only_one_seed_and_do_not_restore_revoked_units(self):
        other = SqliteConnection(self.path)
        try:
            await asyncio.gather(self.initialize(), self.initialize(other))
            self.assertEqual(len(self.state()["_function_module_versions"]), 4)
            await self.revoke()
            before = self.state()
            await asyncio.gather(self.initialize(), self.initialize(other))
            self.assertEqual(self.state(), before)
            for conn in (self.conn, other):
                marker_lock = next(i for i, sql in enumerate(conn.calls) if "config_value FROM _system_config" in sql)
                first_module = next(i for i, sql in enumerate(conn.calls) if "FROM _function_modules WHERE" in sql)
                self.assertIn("FOR UPDATE", conn.calls[marker_lock])
                self.assertLess(marker_lock, first_module)
        finally:
            other.raw.close()

    async def test_invalid_marker_fails_closed_and_does_not_write_modules(self):
        self.conn.raw.execute("INSERT INTO _system_config VALUES('function_module_units_seeded','invalid')")
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, "种子标记无效"):
            await self.initialize()
        self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
