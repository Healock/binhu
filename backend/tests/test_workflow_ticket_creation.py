import os
import unittest
from fastapi import HTTPException

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.workflow import TicketCreate, create_ticket


class WorkflowTicketCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_request_is_rejected_before_database_access(self):
        ticket = TicketCreate(
            type_code="photo_request",
            title="调取照片",
            form_data={},
        )
        with self.assertRaises(HTTPException) as raised:
            await create_ticket(ticket, user={"id": 7}, conn=object())
        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(raised.exception.detail, "照片调取业务已下线，停止创建新申请")


if __name__ == "__main__":
    unittest.main()
