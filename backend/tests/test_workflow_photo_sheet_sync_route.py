from __future__ import annotations

import os
from unittest.mock import AsyncMock, Mock, patch

import pytest
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.workflow_photo_sheet import retry_photo_sheet_outbox, run_photo_sheet_sync


@pytest.mark.asyncio
async def test_manual_sync_returns_background_run_and_preserves_order():
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
        result = await run_photo_sheet_sync(
            request=request,
            full=True,
            user={"id": 7, "username": "synthetic-admin"},
        )

    assert result["run"]["id"] == 12
    assert create_job.await_count == 1


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
        if self.query.startswith("SELECT work_order_id,status"):
            return (321, "paused")
        return None


class _RetryConnection:
    def __init__(self):
        self.cursor_value = _RetryCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _CursorContext(self.cursor_value)

    async def begin(self):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_manual_outbox_retry_resets_attempts_and_launches_processing():
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/workflow/photo-sheet/outbox/12/retry",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    })
    connection = _RetryConnection()
    launch = Mock()
    with (
        patch(
            "routers.workflow_photo_sheet.record_admin_audit",
            new=AsyncMock(),
        ),
        patch(
            "routers.workflow_photo_sheet.launch_outbox_processing",
            new=launch,
        ),
    ):
        result = await retry_photo_sheet_outbox(
            outbox_id=12,
            request=request,
            user={"id": 7, "username": "synthetic-admin"},
            conn=connection,
        )

    reset_sql = next(
        query for query, _params in connection.cursor_value.executed
        if query.startswith("UPDATE photo_sheet_outbox")
    )
    assert "status='pending'" in reset_sql
    assert "attempt_count=0" in reset_sql
    assert connection.commits == 1
    launch.assert_called_once_with(321)
    assert result["status"] == "pending"
