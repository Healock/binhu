from __future__ import annotations

import os
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.workflow_photo_sheet import (
    retry_photo_sheet_conflict,
    retry_photo_sheet_outbox,
    run_photo_sheet_sync,
)


@pytest.mark.asyncio
async def test_manual_tencent_photo_sync_is_retired_without_creating_a_job():
    order: list[str] = []

    async def outbox_once():
        order.append("outbox")
        return {"processed": 1, "failed": 0}

    async def sync_once(*, full: bool, actor_user_id: int):
        order.append("full" if full else "incremental")
        return {"created_tickets": 0, "completed_tickets": 0}

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/workflow/photo-sheet/sync",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    })
    with (
        patch(
            "routers.workflow_photo_sheet.process_outbox_once",
            new=AsyncMock(side_effect=outbox_once),
        ),
        patch(
            "routers.workflow_photo_sheet.sync_online_once",
            new=AsyncMock(side_effect=sync_once),
        ),
        patch(
            "routers.workflow_photo_sheet.record_admin_audit",
            new=AsyncMock(),
        ),
        patch(
            "routers.workflow_photo_sheet.create_job",
            new=AsyncMock(return_value=({"id": 12, "status": "queued"}, False)),
        ) as create_job,
    ):
        with pytest.raises(HTTPException) as raised:
            await run_photo_sheet_sync(
                request=request,
                full=True,
                user={"id": 7, "username": "synthetic-admin"},
            )

    assert raised.value.status_code == 409
    assert "腾讯数据源已下线" in raised.value.detail
    assert create_job.await_count == 0
    assert order == []


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _RetryCursor:
    def __init__(self):
        self.query = ""
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query, params=()):
        self.query = " ".join(query.split())
        self.executed.append((self.query, params))

    async def fetchone(self):
        if self.query.startswith("SELECT work_order_id,status,conflict_type"):
            return (321, "pending", "row_location")
        if self.query.startswith("SELECT work_order_id,status"):
            return (321, "paused", "quota_exhausted")
        return None


class _RetryConnection:
    def __init__(self):
        self.cursor_value = _RetryCursor()
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _CursorContext(self.cursor_value)

    async def begin(self):
        self.begins += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_manual_outbox_retry_is_rejected_in_local_mode_without_mutation_or_external_processing():
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/workflow/photo-sheet/outbox/12/retry",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    })
    connection = _RetryConnection()
    launch = Mock()
    audit = AsyncMock()
    with (
        patch(
            "routers.workflow_photo_sheet.record_admin_audit",
            new=audit,
        ),
        patch(
            "routers.workflow_photo_sheet.launch_outbox_processing",
            new=launch,
        ),
    ):
        with pytest.raises(HTTPException) as raised:
            await retry_photo_sheet_outbox(
                outbox_id=12,
                request=request,
                user={"id": 7, "username": "synthetic-admin"},
                conn=connection,
            )

    assert raised.value.status_code == 409
    assert "腾讯数据源已下线" in raised.value.detail
    assert connection.begins == 0
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert connection.cursor_value.executed == []
    audit.assert_not_awaited()
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_manual_conflict_retry_is_rejected_in_local_mode_without_mutation_or_external_processing():
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/workflow/photo-sheet/conflicts/12/retry",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    })
    connection = _RetryConnection()
    launch = Mock()
    audit = AsyncMock()
    with (
        patch(
            "routers.workflow_photo_sheet.record_admin_audit",
            new=audit,
        ),
        patch(
            "routers.workflow_photo_sheet.launch_outbox_processing",
            new=launch,
        ),
    ):
        with pytest.raises(HTTPException) as raised:
            await retry_photo_sheet_conflict(
                conflict_id=12,
                request=request,
                user={"id": 7, "username": "synthetic-admin"},
                conn=connection,
            )

    assert raised.value.status_code == 409
    assert "腾讯数据源已下线" in raised.value.detail
    assert connection.begins == 0
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert connection.cursor_value.executed == []
    audit.assert_not_awaited()
    launch.assert_not_called()
