import os
import asyncio
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.local_source import (
    LOCAL_SPREADSHEET_ID,
    local_data_source_enabled,
    local_row_hash,
    local_sheet_id,
    stable_json,
)
from services import admin_task_queue
from services import photo_sheet_sync, sync_tasks
from config import settings


class LocalSourceHelpersTest(unittest.TestCase):
    def test_local_locator_is_stable_and_not_a_tencent_position(self):
        self.assertEqual(LOCAL_SPREADSHEET_ID, 0)
        self.assertEqual(local_sheet_id("全链条"), "local:全链条")

    def test_stable_json_and_hash_are_order_independent(self):
        left = {"身份证号": "320000000000000000", "社区": "长板"}
        right = {"社区": "长板", "身份证号": "320000000000000000"}
        self.assertEqual(stable_json(left), stable_json(right))
        self.assertEqual(local_row_hash(left), local_row_hash(right))

    def test_feature_switch_is_read_from_settings(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            self.assertTrue(local_data_source_enabled())
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", False):
            self.assertFalse(local_data_source_enabled())

    def test_local_mode_hides_legacy_external_queues(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            self.assertEqual(asyncio.run(admin_task_queue._sync_jobs()), [])
            self.assertEqual(asyncio.run(admin_task_queue._writeback_queues()), [])
            details = asyncio.run(
                admin_task_queue.get_admin_task_queue_details(
                    "online_writeback_queue"
                )
            )
        self.assertEqual(details["total"], 0)
        self.assertIn("本地数据源已启用", details["message"])

    def test_local_mode_rejects_new_tencent_sync_tasks(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            result = asyncio.run(sync_tasks.create_sync_task("manual", requested_by=7))
        self.assertEqual(result, (0, "disabled", "腾讯数据源已下线，无需创建同步任务"))

    def test_local_mode_does_not_claim_scheduled_tencent_sync_tasks(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            result = asyncio.run(sync_tasks.claim_due_scheduled_task())
        self.assertIsNone(result)

    def test_local_mode_disables_photo_sheet_operations_without_external_access(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            outbox = asyncio.run(photo_sheet_sync.process_outbox_once())
            sync = asyncio.run(photo_sheet_sync.sync_online_once())
            self.assertFalse(photo_sheet_sync.launch_daily_full_sync())
        self.assertTrue(outbox["disabled"])
        self.assertTrue(sync["disabled"])


if __name__ == "__main__":
    unittest.main()
