import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from deps import require_admin_account
from routers.admin_task_queue import router
from services.admin_task_queue import (
    _item,
    get_admin_task_queue_details,
    normalize_task_state,
)


class AdminTaskQueueTests(unittest.TestCase):
    def test_task_states_are_normalized_for_one_frontend_contract(self):
        self.assertEqual(normalize_task_state("pending"), "queued")
        self.assertEqual(normalize_task_state("executing"), "running")
        self.assertEqual(normalize_task_state("retryable"), "retrying")
        self.assertEqual(normalize_task_state("partial"), "warning")
        self.assertEqual(normalize_task_state("completed"), "success")
        self.assertEqual(normalize_task_state("failed"), "failed")

    def test_public_item_contains_only_safe_task_summary(self):
        item = _item(
            source="test",
            source_id=7,
            category="数据同步",
            title="测试任务",
            status="running",
            phase="fetching",
            current=5,
            total=20,
            message="已处理 5 条",
            created_at=datetime(2026, 8, 23, 1, 2, 3),
        )

        self.assertEqual(item["id"], "test:7")
        self.assertEqual(item["progress"], 25)
        self.assertTrue(item["active"])
        self.assertEqual(item["created_at"], "2026-08-23T01:02:03Z")
        for forbidden in (
            "payload",
            "result",
            "error_message",
            "username",
            "identity_number",
            "phone",
            "address",
        ):
            self.assertNotIn(forbidden, item)

        self.assertEqual(item["detail_count"], 0)
        self.assertEqual(item["attention_count"], 0)
        self.assertIsNone(item["retry_kind"])

    def test_every_task_queue_route_requires_admin_account(self):
        self.assertTrue(router.routes)
        for route in router.routes:
            self.assertTrue(
                any(
                    dependency.call is require_admin_account
                    for dependency in route.dependant.dependencies
                ),
                route.path,
            )


class AdminTaskQueueDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_online_conflicts_are_described_without_business_values_or_retry(self):
        query = AsyncMock(side_effect=[
            [(1,)],
            [(
                7,
                "全链条",
                "abcdef0123456789",
                "核查结果",
                "conflict",
                2,
                "remote_changed",
                datetime(2026, 8, 23, 8, 0, 0),
            )],
        ])
        with patch("services.admin_task_queue._query_rows", new=query):
            result = await get_admin_task_queue_details("online_writeback_queue")

        detail = result["data"][0]
        self.assertFalse(detail["can_retry"])
        self.assertIsNone(detail["retry_kind"])
        self.assertIn("腾讯端内容已被其他人修改", detail["diagnosis"])
        self.assertNotIn("base_value", detail)
        self.assertNotIn("local_value", detail)
        self.assertNotIn("remote_value", detail)

    async def test_paused_photo_writeback_allows_only_the_named_safe_retry(self):
        query = AsyncMock(side_effect=[
            [(1,)],
            [(
                18,
                42,
                "complete",
                "paused",
                5,
                "quota_exhausted",
                datetime(2026, 8, 23, 8, 0, 0),
            )],
        ])
        with patch("services.admin_task_queue._query_rows", new=query):
            result = await get_admin_task_queue_details("photo_writeback_queue")

        detail = result["data"][0]
        self.assertTrue(detail["can_retry"])
        self.assertEqual(detail["retry_kind"], "photo_outbox")
        self.assertEqual(detail["reference"], "工单 #42")


if __name__ == "__main__":
    unittest.main()
