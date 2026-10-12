from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers import registry_extended


class _Cursor:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=()):
        self.calls.append((sql, params))

    async def fetchone(self):
        return (0,)

    async def fetchall(self):
        return []


class _Connection:
    def __init__(self):
        self.cursor_instance = _Cursor()

    def cursor(self):
        return self.cursor_instance


@pytest.mark.asyncio
async def test_issue_search_matches_issue_id_and_source_ref_with_parameterized_values(monkeypatch):
    monkeypatch.setattr(registry_extended, "_allowed_community_ids", AsyncMock(return_value=None))
    connection = _Connection()

    result = await registry_extended._registry_import_issue_search_result(
        registry_extended.RegistryIssueSearch(keyword="52498", status=""),
        {"permissions": []},
        connection,
    )

    count_sql, count_params = connection.cursor_instance.calls[0]
    assert "CAST(id AS CHAR) LIKE %s" in count_sql
    assert "source_ref LIKE %s" in count_sql
    assert count_params == ("%52498%",) * 8
    assert result["total"] == 0
