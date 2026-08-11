import json
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.workflow import TicketCreate, create_ticket


class _CheckingCursor:
    def __init__(self):
        self.lastrowid = 0
        self._query = ""
        self.executed: list[tuple[str, tuple | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, query, params=None):
        normalized_params = tuple(params) if params is not None else None
        if normalized_params is not None and query.count("%s") != len(normalized_params):
            raise TypeError("SQL placeholder count does not match parameter count")
        self._query = query
        self.executed.append((query, normalized_params))
        if "INSERT INTO work_orders" in query:
            self.lastrowid = 42

    async def fetchone(self):
        if "FROM workflow_types" in self._query:
            return (1, 24)
        if "FROM workflow_type_versions" in self._query:
            return (2, json.dumps({"fields": []}, ensure_ascii=False))
        if "._communities community" in self._query:
            return ("冬梅社区",)
        return None

    async def fetchall(self):
        if "FROM workflow_steps" in self._query:
            return [
                (3, 1, "基础管控", "process", 24, json.dumps({"queue": "基础管控"}, ensure_ascii=False))
            ]
        return []


class _FakeConnection:
    def __init__(self):
        self.cursor_instance = _CheckingCursor()
        self.committed = False
        self.rolled_back = False

    async def begin(self):
        return None

    def cursor(self):
        return self.cursor_instance

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class WorkflowTicketCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_photo_request_insert_has_matching_placeholders(self):
        conn = _FakeConnection()
        ticket = TicketCreate(
            type_code="photo_request",
            title="调取照片",
            form_data={
                "subject_name": "测试人员",
                "identity_number": "00000020000101000X",
                "source_parser_type": "全链条",
                "source_row_key": "synthetic-row-key",
                "community_name": "冬梅社区",
                "source_label": "全链条",
            },
        )
        user = {
            "id": 7,
            "username": "synthetic-user",
            "display_name": "测试申请人",
            "member": {"name": "测试申请人"},
        }

        with patch("routers.workflow.hmac_digest", return_value=("synthetic-hmac", 1)), \
             patch("routers.workflow.queue_user_ids", new=AsyncMock(return_value=[])), \
             patch("routers.workflow.workflow_notification", new=AsyncMock()), \
             patch("routers.workflow.enqueue_outbox", new=AsyncMock()):
            result = await create_ticket(ticket, user=user, conn=conn)

        self.assertEqual(result["id"], 42)
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)
        detail_insert = next(
            (query, params)
            for query, params in conn.cursor_instance.executed
            if "INSERT INTO photo_request_details" in query
        )
        self.assertEqual(detail_insert[0].count("%s"), 18)
        self.assertEqual(len(detail_insert[1]), 18)


if __name__ == "__main__":
    unittest.main()
