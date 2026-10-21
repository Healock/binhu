import unittest
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from migrations import photo_sheet_retirement
from routers.workflow import Decision, TicketCreate, _can_view_ticket, create_ticket, decide_ticket
from services import photo_sheet_sync


class _Connection:
    def __init__(self, cursor=None):
        self.rollback = AsyncMock()
        self.commit = AsyncMock()
        self.close = lambda: None
        self._cursor = cursor

    async def begin(self):
        return None

    def cursor(self):
        return _CursorContext(self._cursor)


class _CursorContext:
    def __init__(self, cursor=None):
        self.cursor = cursor or object()

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ResultCursor:
    def __init__(self, row):
        self.row = row
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((statement, params))

    async def fetchone(self):
        return self.row


class _DeleteCursor:
    def __init__(self):
        self.statements = []
        self.rowcount = 0

    async def execute(self, statement, params=None):
        self.statements.append((statement, params))
        self.rowcount = 1


class _AttachmentInventoryCursor:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((statement, params))

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return (0,)


class PhotoSheetRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_ticket_guard_stops_before_database_access(self):
        ticket = TicketCreate(type_code="photo_request", title="历史照片", form_data={})
        with self.assertRaises(HTTPException) as raised:
            await create_ticket(ticket, user={"id": 1}, conn=object())
        self.assertEqual(raised.exception.status_code, 410)

    async def test_generic_ticket_detail_rejects_retired_photo_ticket(self):
        cursor = _ResultCursor(("photo_request",))
        with self.assertRaises(HTTPException) as raised:
            await _can_view_ticket(cursor, 42, {"id": 1})
        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(len(cursor.statements), 1)

    async def test_generic_decision_rejects_retired_photo_ticket(self):
        cursor = _ResultCursor(("queued", None, 1, 7, "photo_request", "队列"))
        conn = _Connection(cursor)
        with patch("routers.workflow.record_admin_audit", new=AsyncMock()):
            with self.assertRaises(HTTPException) as raised:
                await decide_ticket(
                    42,
                    Decision(expected_version=1, action="complete"),
                    request=object(),
                    user={"id": 7, "permissions": []},
                    conn=conn,
                )
        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(len(cursor.statements), 1)
        conn.rollback.assert_awaited_once()

    async def test_empty_photo_ticket_list_never_builds_an_empty_in_clause(self):
        cursor = _DeleteCursor()
        with patch.object(photo_sheet_retirement, "table_exists", new=AsyncMock(return_value=True)):
            counts = await photo_sheet_retirement.delete_photo_rows(cursor, [])
        self.assertEqual(set(counts), set(photo_sheet_retirement.PHOTO_TABLES))
        self.assertEqual(len(cursor.statements), len(photo_sheet_retirement.PHOTO_TABLES))
        self.assertTrue(all(" IN ()" not in sql for sql, _ in cursor.statements))

    async def test_measure_file_reconciliation_checks_sha256(self):
        content = b"synthetic photo attachment"
        correct = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "synthetic.bin"
            file_path.write_bytes(content)
            mismatch = "0" * 64 if correct != "0" * 64 else "1" * 64
            with patch.object(photo_sheet_retirement, "resolve_attachment", return_value=file_path):
                result = photo_sheet_retirement._file_reference_reconciliation(
                    {"42/synthetic.bin": {mismatch}}, photo_sheet_retirement.resolve_attachment,
                )
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["sha256_mismatches"], 1)

    async def test_measure_returns_attachment_and_zip_reconciliation_counts(self):
        cursor = _ResultCursor((3,))
        with patch.object(photo_sheet_retirement, "photo_ticket_ids", new=AsyncMock(return_value=[42])), \
             patch.object(photo_sheet_retirement, "count_table", new=AsyncMock(return_value=0)), \
             patch.object(photo_sheet_retirement, "attachment_inventory", new=AsyncMock(return_value={
                 "photo_attachment_rows": 1,
                 "attachment_keys": ["42/photo.jpg"],
                 "attachment_hashes": {"42/photo.jpg": {"a" * 64}},
                 "invalid_storage_key_rows": 0,
                 "shared_attachment_rows": 0,
             })), \
             patch.object(photo_sheet_retirement, "zip_inventory", new=AsyncMock(return_value={
                 "hashes": {"batch.zip": {"b" * 64}},
                 "invalid_storage_key_rows": 0,
             })), \
             patch.object(photo_sheet_retirement, "_file_reference_reconciliation", side_effect=[
                 {"referenced": 1, "checked": 1, "missing": 0, "sha256_mismatches": 0,
                  "invalid_sha256": 0, "conflicting_sha256": 0},
                 {"referenced": 1, "checked": 1, "missing": 0, "sha256_mismatches": 1,
                  "invalid_sha256": 0, "conflicting_sha256": 0},
             ]), \
             patch.object(photo_sheet_retirement, "_load_retirement_state", return_value={
                 "pending_runs": 0, "orphan_stage_directories": 0,
                 "file_residue_count": 0, "invalid_state_files": 0,
             }):
            result = await photo_sheet_retirement.measure_with_cursor(cursor)
        self.assertEqual(result["file_reconciliation"]["attachments"]["checked"], 1)
        self.assertEqual(result["file_reconciliation"]["photo_import_zips"]["sha256_mismatches"], 1)

    async def test_attachment_inventory_hashes_only_live_files_but_counts_soft_deleted_rows(self):
        cursor = _AttachmentInventoryCursor([
            ("deleted/photo.jpg", "a" * 64, 1, 0),
            ("active/photo.jpg", "b" * 64, 1, 1),
            ("", "c" * 64, 1, 1),
        ])
        with patch.object(photo_sheet_retirement, "table_exists", new=AsyncMock(return_value=True)):
            result = await photo_sheet_retirement.attachment_inventory(cursor, [42])

        self.assertEqual(result["photo_attachment_rows"], 3)
        self.assertEqual(result["attachment_keys"], ["active/photo.jpg"])
        self.assertEqual(result["attachment_hashes"], {"active/photo.jpg": {"b" * 64}})
        self.assertEqual(result["invalid_storage_key_rows"], 1)
        self.assertIn("attachment.deleted_at IS NULL", cursor.statements[1][0])

    async def test_discard_stage_failure_is_persisted_for_verify(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_dir = Path(temp_dir) / "attachments"
            attachment_dir.mkdir()
            stage = attachment_dir / ".photo-sheet-retirement" / ("a" * 32)
            stage.mkdir(parents=True)
            run_id = "a" * 32
            state = {"kind": "photo_sheet_retirement", "run_id": run_id, "files": []}
            with patch.object(photo_sheet_retirement.settings, "WORKFLOW_ATTACHMENT_DIR", str(attachment_dir)), \
                 patch.object(photo_sheet_retirement, "_discard_stage", side_effect=OSError("synthetic cleanup error")):
                completed, error = photo_sheet_retirement._discard_stage_with_state(run_id, stage, state)
                recorded = json.loads(photo_sheet_retirement._state_path(run_id).read_text(encoding="utf-8"))
                reconciliation = photo_sheet_retirement._load_retirement_state()
        self.assertFalse(completed)
        self.assertEqual(error, "OSError")
        self.assertEqual(recorded["status"], "database_committed_cleanup_pending")
        self.assertEqual(reconciliation["pending_runs"], 1)

    async def test_verify_fails_when_cleanup_state_is_pending(self):
        conn = _Connection()
        measured = {
            "photo_work_orders": 0,
            "table_counts": {table: 0 for table in photo_sheet_retirement.PHOTO_TABLES},
            "photo_attachment_rows": 0,
            "shared_attachment_rows": 0,
            "photo_zip_file_count": 0,
            "file_reconciliation": {
                "attachments": {"missing": 0, "sha256_mismatches": 0, "invalid_sha256": 0,
                                 "conflicting_sha256": 0, "invalid_storage_key_rows": 0},
                "photo_import_zips": {"missing": 0, "sha256_mismatches": 0, "invalid_sha256": 0,
                                       "conflicting_sha256": 0, "invalid_storage_key_rows": 0},
                "cleanup_state": {"pending_runs": 1, "orphan_stage_directories": 0,
                                  "file_residue_count": 0, "invalid_state_files": 0},
            },
        }
        with patch.object(photo_sheet_retirement, "open_connection", new=AsyncMock(return_value=conn)), \
             patch.object(photo_sheet_retirement, "measure_with_cursor", new=AsyncMock(return_value=measured)):
            result = await photo_sheet_retirement.run_verify()
        self.assertFalse(result["checks"]["cleanup_state_clear"])
        self.assertFalse(result["ok"])

    async def test_apply_stops_when_photo_ticket_count_does_not_match_explicit_guard(self):
        conn = _Connection()
        measured = {"photo_work_orders": 133}
        with patch.object(photo_sheet_retirement, "open_connection", new=AsyncMock(return_value=conn)), \
             patch.object(photo_sheet_retirement, "measure_with_cursor", new=AsyncMock(return_value=measured)):
            with self.assertRaisesRegex(RuntimeError, "expected=134, actual=133"):
                await photo_sheet_retirement.run_apply(
                    expected_photo_tickets=134,
                    confirm_empty_photo_tables=True,
                )
        conn.rollback.assert_awaited_once()

    async def test_apply_requires_explicit_photo_table_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "confirm-empty-photo-tables"):
            await photo_sheet_retirement.run_apply(
                expected_photo_tickets=134,
                confirm_empty_photo_tables=False,
            )

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
