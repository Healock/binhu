import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services import audit, work_activity


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return None


class _Cursor:
    def __init__(self):
        self.execute = AsyncMock()
        self.lastrowid = 17
        self.rowcount = 1


class _Connection:
    def __init__(self):
        self.cursor_value = _Cursor()

    def cursor(self):
        return _Context(self.cursor_value)


class SecondaryWritePoolingTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_audit_reuses_supplied_connection(self):
        connection = _Connection()
        with patch.object(
            audit.db_manager,
            "get_pool",
            side_effect=AssertionError("must not acquire the same pool twice"),
        ):
            result = await audit.record_admin_audit(
                {"id": 1, "username": "loadtest-user"},
                "online.local_update",
                conn=connection,
            )
        self.assertEqual(result, 17)
        connection.cursor_value.execute.assert_awaited_once()

    async def test_work_activity_reuses_supplied_connection(self):
        connection = _Connection()
        user = {"id": 1, "member": {"id": 2}}
        with patch.object(
            work_activity.db_manager,
            "get_pool",
            side_effect=AssertionError("must not acquire the same pool twice"),
        ):
            result = await work_activity.record_work_activity(
                user,
                work_activity.ONLINE_TASK_UPDATE,
                event_key="local:17",
                conn=connection,
            )
        self.assertTrue(result)
        connection.cursor_value.execute.assert_awaited_once()

    async def test_admin_audit_pool_acquire_is_bounded(self):
        pool = MagicMock()
        pool.acquire.return_value = object()
        with (
            patch.object(audit.db_manager, "get_pool", return_value=pool),
            patch.object(audit.asyncio, "wait_for", new=AsyncMock(side_effect=TimeoutError)),
        ):
            with self.assertRaises(TimeoutError):
                await audit.record_admin_audit(None, "test")

    async def test_work_activity_pool_timeout_is_non_blocking_failure(self):
        pool = MagicMock()
        pool.acquire.return_value = object()
        user = {"id": 1, "member": None}
        with (
            patch.object(work_activity.db_manager, "get_pool", return_value=pool),
            patch.object(
                work_activity.asyncio,
                "wait_for",
                new=AsyncMock(side_effect=TimeoutError),
            ),
        ):
            result = await work_activity.record_work_activity(
                user,
                work_activity.ONLINE_TASK_UPDATE,
                event_key="local:18",
            )
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
