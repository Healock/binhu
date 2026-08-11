from datetime import datetime, timedelta
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from deps import require_admin
from routers.sync import SyncScheduleRequest
from schemas.sync import SyncScheduleStatus
from services import sync_tasks


class FakeCursor:
    def __init__(
        self,
        *,
        schedule=(1, 5, None),
        now=None,
        active_task=None,
        interrupted=None,
    ):
        self.schedule = list(schedule)
        self.now = now or datetime(2026, 7, 27, 8, 0, 0)
        self.active_task = active_task
        self.interrupted = interrupted or []
        self.executed = []
        self.last_sql = ""
        self.lastrowid = 77
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.last_sql = normalized
        self.executed.append((normalized, params))
        if normalized.startswith("UPDATE _sync_schedule SET last_triggered_at"):
            self.schedule[2] = self.now + timedelta(
                minutes=self.schedule[1]
            )

    async def fetchone(self):
        if self.last_sql.startswith("SELECT GET_LOCK"):
            return (1,)
        if self.last_sql.startswith("SELECT RELEASE_LOCK"):
            return (1,)
        if self.last_sql.startswith(
            "SELECT enabled, interval_minutes, next_run_at"
        ):
            return tuple(self.schedule)
        if self.last_sql == "SELECT UTC_TIMESTAMP()":
            return (self.now,)
        if self.last_sql.startswith("SELECT id FROM _sync_log"):
            return (self.active_task,) if self.active_task else None
        return None

    async def fetchall(self):
        if self.last_sql.startswith("SELECT id, trigger_source FROM _sync_log"):
            return list(self.interrupted)
        if self.last_sql.startswith(
            "SELECT parser_type FROM _config_spreadsheets"
        ):
            return []
        return []


class FakePool:
    def __init__(self, cursor):
        self.cursor = cursor
        self.connection = MagicMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=cursor)
        context.__aexit__ = AsyncMock(return_value=None)
        self.connection.cursor.return_value = context
        self.acquire = AsyncMock(return_value=self.connection)
        self.release = MagicMock()


class ScheduledSyncTests(unittest.IsolatedAsyncioTestCase):
    def test_schedule_defaults_to_enabled_every_five_minutes(self):
        schedule = SyncScheduleStatus()
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.interval_minutes, 5)

    async def test_manual_sync_permission_allows_only_admin_roles(self):
        for role in ("admin", "super_admin"):
            user = {"id": 1, "role": role}
            self.assertEqual(await require_admin(user), user)

        for role in ("leader", "member"):
            with self.assertRaises(HTTPException) as raised:
                await require_admin({"id": 2, "role": role})
            self.assertEqual(raised.exception.status_code, 403)

    def test_schedule_interval_validation(self):
        self.assertEqual(
            SyncScheduleRequest(enabled=True, interval_minutes=5).interval_minutes,
            5,
        )
        self.assertEqual(
            SyncScheduleRequest(
                enabled=True,
                interval_minutes=10080,
            ).interval_minutes,
            10080,
        )
        for invalid in (4, 10081):
            with self.assertRaises(ValidationError):
                SyncScheduleRequest(
                    enabled=True,
                    interval_minutes=invalid,
                )

    async def test_due_schedule_is_claimed_only_once(self):
        now = datetime(2026, 7, 27, 8, 0, 0)
        cursor = FakeCursor(
            schedule=(1, 5, now - timedelta(seconds=1)),
            now=now,
        )
        pool = FakePool(cursor)

        with patch.object(
            sync_tasks.db_manager,
            "get_pool",
            return_value=pool,
        ):
            first = await sync_tasks.claim_due_scheduled_task()
            second = await sync_tasks.claim_due_scheduled_task()

        self.assertEqual(first, 77)
        self.assertIsNone(second)
        inserts = [
            sql
            for sql, _ in cursor.executed
            if sql.startswith("INSERT INTO _sync_log")
        ]
        self.assertEqual(len(inserts), 1)

    async def test_due_schedule_does_not_duplicate_active_task(self):
        now = datetime(2026, 7, 27, 8, 0, 0)
        cursor = FakeCursor(
            schedule=(1, 5, now - timedelta(seconds=1)),
            now=now,
            active_task=41,
        )
        pool = FakePool(cursor)

        with patch.object(
            sync_tasks.db_manager,
            "get_pool",
            return_value=pool,
        ):
            result = await sync_tasks.claim_due_scheduled_task()

        self.assertIsNone(result)
        self.assertFalse(
            any(
                sql.startswith("INSERT INTO _sync_log")
                for sql, _ in cursor.executed
            )
        )

    async def test_update_schedule_resets_countdown_and_validates_range(self):
        cursor = FakeCursor()
        pool = FakePool(cursor)
        current = {
            "enabled": True,
            "interval_minutes": 15,
            "next_run_at": "2026-07-27T08:15:00Z",
            "server_time": "2026-07-27T08:00:00Z",
        }

        with patch.object(
            sync_tasks.db_manager,
            "get_pool",
            return_value=pool,
        ), patch.object(
            sync_tasks,
            "get_schedule",
            new=AsyncMock(return_value=current),
        ):
            result = await sync_tasks.update_schedule(True, 15, 3)

        self.assertEqual(result, current)
        update = next(
            (params for sql, params in cursor.executed
             if sql.startswith("UPDATE _sync_schedule SET enabled=")),
            None,
        )
        self.assertEqual(update, (1, 15, 3, 1, 15))

        for invalid in (4, 10081):
            with self.assertRaises(ValueError):
                await sync_tasks.update_schedule(True, invalid, 3)

    async def test_task_completion_resets_schedule_and_notifies_auto_failures(self):
        cases = [
            ("scheduled", "failed", 1),
            ("scheduled", "partial", 1),
            ("scheduled", "success", 1),
            ("manual", "failed", 0),
        ]
        for trigger_source, status, notice_count in cases:
            with self.subTest(trigger_source=trigger_source, status=status):
                engine = MagicMock()
                engine.run_full_sync = AsyncMock()
                reset = AsyncMock()
                notify = AsyncMock()
                with patch.object(
                    sync_tasks,
                    "SyncEngine",
                    return_value=engine,
                ), patch.object(
                    sync_tasks.db_manager,
                    "get_pool",
                    return_value=MagicMock(),
                ), patch.object(
                    sync_tasks,
                    "_get_task_terminal_state",
                    new=AsyncMock(
                        return_value=(status, trigger_source, "test error")
                    ),
                ), patch.object(
                    sync_tasks,
                    "reset_next_run_from_now",
                    new=reset,
                ), patch.object(
                    sync_tasks,
                    "create_sync_status_notifications",
                    new=notify,
                ):
                    await sync_tasks.run_sync_task(88)

                reset.assert_awaited_once()
                self.assertEqual(notify.await_count, notice_count)

    async def test_startup_recovery_marks_tasks_failed_and_notifies_auto_only(self):
        cursor = FakeCursor(
            interrupted=[(11, "scheduled"), (12, "manual")],
        )
        pool = FakePool(cursor)
        reset = AsyncMock()
        notify = AsyncMock()

        with patch.object(
            sync_tasks.db_manager,
            "get_pool",
            return_value=pool,
        ), patch.object(
            sync_tasks,
            "reset_next_run_from_now",
            new=reset,
        ), patch.object(
            sync_tasks,
            "create_sync_failure_notifications",
            new=notify,
        ):
            count = await sync_tasks.recover_interrupted_tasks()

        self.assertEqual(count, 2)
        self.assertTrue(
            any(
                sql.startswith("UPDATE _sync_log SET status='failed'")
                for sql, _ in cursor.executed
            )
        )
        reset.assert_awaited_once()
        notify.assert_awaited_once()
        self.assertEqual(notify.await_args.args[0], 11)


if __name__ == "__main__":
    unittest.main()
