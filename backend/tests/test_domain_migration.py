from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from migrations import domain_migration


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))

    async def fetchall(self):
        return list(self.rows)


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor

    def cursor(self):
        return _CursorContext(self.cursor_value)

    def close(self):
        pass

    async def wait_closed(self):
        pass


def test_primary_key_columns_preserves_composite_order():
    cursor = _Cursor([("first",), ("second",)])
    result = asyncio.run(domain_migration.primary_key_columns(cursor, "OnlineData", "example"))
    assert result == ["first", "second"]
    assert len(cursor.calls) == 1


def test_migrate_dry_run_does_not_create_state_or_copy_business_tables():
    cursor = _Cursor()
    connection = _Connection(cursor)
    with patch.object(domain_migration, "open_connection", new=AsyncMock(return_value=connection)):
        result = asyncio.run(domain_migration.migrate_domain("visit", apply=False, chunk_size=100))
    assert result["apply"] is False
    assert all(item["status"] == "dry_run" for item in result["tables"])
    assert cursor.calls == []
