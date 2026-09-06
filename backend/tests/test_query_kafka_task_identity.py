import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers import query


class FakeCursor:
    def __init__(self, *, source_row=None):
        self.calls = []
        self.lastrowid = 27
        self.rowcount = 1
        self._source_row = source_row
        self._sql = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, sql, params=None):
        self._sql = str(sql)
        self.calls.append((self._sql, params))

    async def fetchone(self):
        compact = " ".join(self._sql.split())
        if "SELECT id, physical_row FROM _online_source_rows" in compact:
            return (904, 27)
        if "SELECT id, revision FROM _online_source_rows" in compact:
            return (904, 1)
        if "FROM _online_source_rows AS source" in compact:
            return self._source_row
        return None


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.begin = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    def cursor(self):
        return self._cursor


def _create_values():
    return {
        "下发日期": "2026-09-07",
        "截止日期": "2026-09-08",
        "社区": "虚构社区",
        "身份证号": "320000000000000000",
        "电话号码": "13900000000",
    }


def _request():
    return SimpleNamespace(headers={}, client=None)


@pytest.mark.asyncio
async def test_create_task_event_uses_business_id_and_source_id_separately(monkeypatch):
    cursor = FakeCursor()
    conn = FakeConn(cursor)
    event = AsyncMock()
    monkeypatch.setattr(query, "local_data_source_enabled", lambda: True)
    monkeypatch.setattr(query, "validate_new_row_scope", AsyncMock(return_value=None))
    monkeypatch.setattr(query, "rebuild_projection_rows", AsyncMock())
    monkeypatch.setattr(query, "enqueue_task_event", event)
    monkeypatch.setattr(query, "record_admin_audit", AsyncMock())

    result = await query.create_source_row(
        "全链条",
        query.SourceRowCreate(values=_create_values()),
        request=_request(),
        user={"id": 1},
        conn=conn,
    )

    kwargs = event.await_args.kwargs
    assert kwargs["task_id"] == "t_fullchain:27"
    assert kwargs["source_id"] == 904
    assert result["source_id"] == 904
    assert result["revision"] == 1
    assert kwargs["task_id"].rsplit(":", 1)[1] != str(kwargs["source_id"])


def _delete_source_row():
    return (
        904, 0, "local:全链条", 27, "row-key", "hash", "{}", "{}", 4,
        None, None, None, 1, None, "local_table", "t_fullchain:27",
    )


@pytest.mark.asyncio
async def test_delete_keeps_domain_deleted_but_requests_explicit_kafka_archive(
    monkeypatch,
):
    cursor = FakeCursor(source_row=_delete_source_row())
    conn = FakeConn(cursor)
    event = AsyncMock()
    monkeypatch.setattr(query, "local_data_source_enabled", lambda: True)
    monkeypatch.setattr(query, "rebuild_projection_rows", AsyncMock())
    monkeypatch.setattr(query, "enqueue_task_event", event)
    monkeypatch.setattr(query, "record_admin_audit", AsyncMock())
    monkeypatch.setattr(query, "can_manage_rows", lambda user: True)
    monkeypatch.setattr(query.settings, "MYSQL_ARCHIVE_DB", "BinhuShadowArchive")

    await query.delete_source_row(
        "全链条", 904, request=_request(), expected_revision=4,
        user={"id": 1, "username": "shadow-admin"}, conn=conn,
    )

    kwargs = event.await_args.kwargs
    assert kwargs["event_type"] == "online.task.deleted"
    assert kwargs["kafka_event_type"] == "task.archived"
    assert kwargs["task_id"] == "t_fullchain:27"
    assert kwargs["source_id"] == 904
    archive_sql = next(sql for sql, _ in cursor.calls if "INSERT INTO" in sql)
    assert "`BinhuShadowArchive`.`t_fullchain_archive`" in archive_sql
    assert "OnlineDataArchive" not in archive_sql


@pytest.mark.asyncio
async def test_delete_rolls_back_when_archive_transaction_fails(monkeypatch):
    cursor = FakeCursor(source_row=_delete_source_row())
    conn = FakeConn(cursor)
    monkeypatch.setattr(query, "local_data_source_enabled", lambda: True)
    monkeypatch.setattr(
        query, "rebuild_projection_rows", AsyncMock(side_effect=RuntimeError("fixture failure"))
    )
    monkeypatch.setattr(query, "enqueue_task_event", AsyncMock())
    monkeypatch.setattr(query, "can_manage_rows", lambda user: True)

    with pytest.raises(RuntimeError, match="fixture failure"):
        await query.delete_source_row(
            "全链条", 904, request=_request(), expected_revision=4,
            user={"id": 1, "username": "shadow-admin"}, conn=conn,
        )

    conn.rollback.assert_awaited_once()
    conn.commit.assert_not_awaited()
