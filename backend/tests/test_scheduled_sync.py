import inspect
import unittest

from fastapi import HTTPException

from routers import sync


class RetiredScheduledSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_trigger_returns_disabled_without_creating_a_task(self):
        result = await sync.trigger_sync(user={"id": 1, "role": "admin"})

        self.assertEqual(result.task_id, 0)
        self.assertEqual(result.status, "disabled")
        self.assertIn("腾讯数据源已下线", result.message)

    async def test_status_and_schedule_are_permanently_disabled(self):
        status = await sync.sync_status(user={"id": 1})
        schedule = await sync.read_schedule(user={"id": 1, "role": "super_admin"})

        self.assertEqual(status.status, "disabled")
        self.assertEqual(status.schedule.enabled, False)
        self.assertEqual(schedule["enabled"], False)
        self.assertEqual(schedule["interval_minutes"], 0)
        self.assertTrue(schedule["disabled"])

    async def test_schedule_update_is_gone(self):
        with self.assertRaises(HTTPException) as raised:
            await sync.save_schedule(user={"id": 1, "role": "super_admin"})

        self.assertEqual(raised.exception.status_code, 410)
        self.assertIn("腾讯数据源已下线", raised.exception.detail)

    async def test_history_endpoint_does_not_read_legacy_sync_log(self):
        result = await sync.sync_history(user={"id": 1})

        self.assertEqual(result["data"], [])
        self.assertEqual(result["total"], 0)
        self.assertIn("腾讯数据源已下线", result["message"])

    def test_retired_router_has_no_scheduler_or_task_creation_dependency(self):
        source = inspect.getsource(sync)

        self.assertNotIn("SyncScheduleRequest", source)
        self.assertNotIn("create_sync_task", source)
        self.assertNotIn("update_schedule", source)
        self.assertNotIn("services.sync_tasks", source)


if __name__ == "__main__":
    unittest.main()
