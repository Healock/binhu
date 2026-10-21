import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
from routers.grid_members import GridMemberLeaveUpdate, update_member_leave
from fastapi import HTTPException


class LeaveCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_past_leave_uses_versioned_service_without_bulk_deactivation(self):
        cur = AsyncMock()
        conn = MagicMock()
        conn.cursor.return_value.__aenter__.return_value = cur
        data = GridMemberLeaveUpdate(
            action="temporary",
            leave_start_date=date(2026, 1, 1),
            leave_end_date=date(2026, 1, 2),
            expected_version=4,
            request_id="request-0000000001",
        )
        with patch(
            "services.business_time.get_business_date",
            AsyncMock(return_value=date(2026, 10, 19)),
        ), patch(
            "routers.grid_members.leave_mutation",
            AsyncMock(return_value={"message": "ok"}),
        ) as mutate:
            await update_member_leave(1, data, user={"id": 9}, conn=conn)
        payload = mutate.await_args.args[1]
        self.assertEqual(payload["expected_version"], 4)
        self.assertEqual(payload["start_date"], date(2026, 1, 1))
        self.assertEqual(payload["action"], "create")

    async def test_clear_requires_selecting_a_record_not_bulk_truncation(self):
        conn = MagicMock()
        data = GridMemberLeaveUpdate(
            action="clear", expected_version=0, request_id="request-0000000001"
        )
        with patch(
            "services.business_time.get_business_date",
            AsyncMock(return_value=date(2026, 10, 19)),
        ):
            with self.assertRaises(HTTPException) as caught:
                await update_member_leave(1, data, user={"id": 9}, conn=conn)
        self.assertEqual(caught.exception.status_code, 409)
