import os
import unittest
from datetime import datetime

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from deps import require_admin_account
from routers.admin_task_queue import router
from services.admin_task_queue import _item, normalize_task_state


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


if __name__ == "__main__":
    unittest.main()
