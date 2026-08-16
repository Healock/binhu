from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from services import workflow_scheduler


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ReminderCursor:
    def __init__(self, reminder_rowcount: int):
        self.reminder_rowcount = reminder_rowcount
        self.rowcount = 0
        self.query = ""
        self.executed: list[str] = []

    async def execute(self, query, params=()):
        del params
        self.query = " ".join(query.split())
        self.executed.append(self.query)
        self.rowcount = (
            self.reminder_rowcount
            if self.query.startswith("INSERT INTO work_order_reminders")
            else 0
        )

    async def fetchall(self):
        if "FROM work_orders" in self.query:
            return [(1, 7, "基础管控", datetime(2026, 1, 1), 1, "{}")]
        return []


class _ReminderConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return _CursorContext(self._cursor)


class _ReminderPool:
    def __init__(self, cursor):
        self.connection = _ReminderConnection(cursor)

    async def acquire(self):
        return self.connection

    def release(self, connection):
        assert connection is self.connection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rowcount", "expected_notifications"),
    [(1, 1), (0, 0)],
)
async def test_reminder_upsert_is_quiet_and_only_notifies_for_new_rows(
    rowcount: int,
    expected_notifications: int,
):
    cursor = _ReminderCursor(rowcount)
    notification = AsyncMock()
    with (
        patch.object(workflow_scheduler.settings, "WORKFLOW_FEATURE_ENABLED", True),
        patch.object(
            workflow_scheduler.db_manager,
            "get_pool",
            return_value=_ReminderPool(cursor),
        ),
        patch.object(
            workflow_scheduler,
            "workflow_notification",
            new=notification,
        ),
    ):
        result = await workflow_scheduler.run_workflow_maintenance_once()

    reminder_sql = next(
        query for query in cursor.executed
        if query.startswith("INSERT INTO work_order_reminders")
    )
    assert "INSERT IGNORE" not in reminder_sql
    assert "ON DUPLICATE KEY UPDATE id=id" in reminder_sql
    assert notification.await_count == expected_notifications
    assert result["reminders"] == expected_notifications


@pytest.mark.asyncio
async def test_workflow_scheduler_cancellation_does_not_enter_photo_maintenance():
    with patch.object(
        workflow_scheduler,
        "run_workflow_maintenance_once",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await workflow_scheduler.run_workflow_scheduler()
