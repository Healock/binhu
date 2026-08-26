from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from database import ARCHIVE_SOURCE_TABLES, ensure_online_archive_schema
from services.parsers import TABLE_NAMES


class ArchiveSchemaCursor:
    def __init__(self):
        self.statements: list[tuple[str, tuple | None]] = []
        self.last_sql = ""
        self.last_params: tuple | None = None

    async def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params
        self.statements.append((sql, params))

    async def fetchone(self):
        if "SHOW COLUMNS" in self.last_sql:
            return None
        if "SHOW INDEX" in self.last_sql and self.last_params == ("uk_row_key",):
            return ("synthetic",)
        if "SHOW INDEX" in self.last_sql and self.last_params == ("idx_row_key",):
            return None
        return None


@pytest.mark.asyncio
async def test_archive_schema_covers_every_registered_online_table():
    cursor = ArchiveSchemaCursor()

    await ensure_online_archive_schema(cursor)

    expected_tables = tuple(dict.fromkeys(TABLE_NAMES.values()))
    assert ARCHIVE_SOURCE_TABLES == expected_tables
    for source_table in expected_tables:
        archive_table = f"{source_table}_archive"
        assert any(
            f"CREATE TABLE IF NOT EXISTS `{archive_table}`" in sql
            and f".`{source_table}`" in sql
            for sql, _params in cursor.statements
        )
        assert any(
            f"ALTER TABLE `{archive_table}` DROP INDEX `uk_row_key`" in sql
            for sql, _params in cursor.statements
        )
        assert any(
            f"ALTER TABLE `{archive_table}` ADD INDEX `idx_row_key` (`_row_key`)" in sql
            for sql, _params in cursor.statements
        )


def test_init_sql_declares_every_registered_archive_table():
    init_sql = (Path(__file__).parents[1] / "init.sql").read_text(encoding="utf-8")

    for source_table in dict.fromkeys(TABLE_NAMES.values()):
        archive_table = f"{source_table}_archive"
        assert (
            f"CREATE TABLE IF NOT EXISTS {archive_table} "
            f"LIKE OnlineData.{source_table};"
        ) in init_sql
