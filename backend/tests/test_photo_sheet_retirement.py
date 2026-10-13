import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from migrations import photo_sheet_retirement
from routers.workflow import TicketCreate, create_ticket
from services import photo_sheet_sync


class _Connection:
    def __init__(self):
        self.rollback = AsyncMock()
        self.close = lambda: None

    async def begin(self):
        return None

    def cursor(self):
        return _CursorContext()


class _CursorContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class PhotoSheetRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_ticket_guard_stops_before_database_access(self):
        ticket = TicketCreate(type_code="photo_request", title="历史照片", form_data={})
        with self.assertRaises(HTTPException) as raised:
            await create_ticket(ticket, user={"id": 1}, conn=object())
        self.assertEqual(raised.exception.status_code, 410)

    async def test_apply_stops_when_photo_ticket_count_is_not_134(self):
        conn = _Connection()
        measured = {"photo_work_orders": 133}
        with patch.object(photo_sheet_retirement, "open_connection", new=AsyncMock(return_value=conn)), \
             patch.object(photo_sheet_retirement, "measure_with_cursor", new=AsyncMock(return_value=measured)):
            with self.assertRaisesRegex(RuntimeError, "expected=134, actual=133"):
                await photo_sheet_retirement.run_apply()
        conn.rollback.assert_awaited_once()

    async def test_sync_entrypoints_are_retired_without_external_access(self):
        self.assertEqual(
            await photo_sheet_sync.sync_online_once(),
            {
                "created_tickets": 0,
                "completed_tickets": 0,
                "revised_count": 0,
                "superseded_count": 0,
                "source_missing_count": 0,
                "disabled": True,
                "retired": True,
            },
        )
        self.assertFalse(photo_sheet_sync.launch_daily_full_sync())
        self.assertTrue((await photo_sheet_sync.process_outbox_once())["retired"])


if __name__ == "__main__":
    unittest.main()
