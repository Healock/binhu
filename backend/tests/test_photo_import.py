import hashlib
import io
import os
import unittest
import zipfile
from unittest.mock import AsyncMock
from unittest.mock import MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.workflow_extended import (
    PhotoRequestBatchClaimPayload,
    PhotoRequestFilterPayload,
    PhotoRequestSearchPayload,
    RestoreQueuedPayload,
    _can_upload_photo_batch,
    _attachment_display_name,
    _photo_matches,
    _photo_duplicate_all,
    _refresh_photo_import_preview,
    _photo_pending_filter,
    _photo_pending_queue,
    batch_claim_photo_requests,
    confirm_photo_import,
    get_photo_import_detail,
    restore_ticket_to_queue,
    router,
)
from routers.workflow_extended import preview_photo_import
from services.photo_import import (
    canonical_photo_filename,
    inspect_photo_zip,
    is_generated_photo_filename,
    parse_photo_filename,
    read_photo_zip_members,
    repair_legacy_zip_text,
)


JPEG = b"\xff\xd8\xffphoto"
PNG = b"\x89PNG\r\n\x1a\nphoto"


def make_zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def make_legacy_gbk_zip(name: str, content: bytes) -> bytes:
    encoded_name = name.encode("gbk")
    placeholder = ("a" * len(encoded_name)).encode("ascii")
    archive = make_zip({placeholder.decode("ascii"): content})
    self_count = archive.count(placeholder)
    if self_count != 2:
        raise AssertionError(f"unexpected ZIP filename occurrence count: {self_count}")
    return archive.replace(placeholder, encoded_name)


class PhotoImportParserTests(unittest.TestCase):
    def test_canonical_photo_filename_uses_jpg_for_jpeg(self):
        self.assertEqual(
            canonical_photo_filename("张三", "32050020000101001x", ".jpeg"),
            "张三_32050020000101001X.jpg",
        )
        self.assertTrue(is_generated_photo_filename("照片-42-deadbeef.jpg"))
        self.assertTrue(is_generated_photo_filename("photo-42-deadbeef"))
        self.assertFalse(is_generated_photo_filename("张三_32050020000101001X.jpg"))
        self.assertEqual(
            _attachment_display_name("photo-42-deadbeef", "image/jpeg", ("张三", "32050020000101001x")),
            "张三_32050020000101001X.jpg",
        )
        self.assertEqual(
            _attachment_display_name("原始附件.jpg", "image/jpeg", ("张三", "32050020000101001x")),
            "原始附件.jpg",
        )

    def test_filename_uses_last_underscore_and_normalizes_x(self):
        safe_name, person_name, identity = parse_photo_filename(
            "张_三_32050020000101001x.jpg"
        )
        self.assertEqual(safe_name, "张_三_32050020000101001x.jpg")
        self.assertEqual(person_name, "张_三")
        self.assertEqual(identity, "32050020000101001X")

    def test_zip_parses_multiple_supported_photos(self):
        parsed = inspect_photo_zip(make_zip({
            "照片/张三_32050020000101001X.jpg": JPEG,
            "李四_320500200001010028.png": PNG,
        }))
        self.assertEqual([item.person_name for item in parsed], ["张三", "李四"])
        self.assertEqual(parsed[0].identity_number, "32050020000101001X")
        self.assertEqual(parsed[1].extension, ".png")

    def test_legacy_gbk_zip_filename_is_recovered(self):
        filename = "张三_32050020000101001X.jpg"
        content = make_legacy_gbk_zip(filename, JPEG)

        parsed = inspect_photo_zip(content)

        self.assertEqual(parsed[0].safe_name, filename)
        self.assertEqual(parsed[0].person_name, "张三")
        self.assertEqual(parsed[0].identity_number, "32050020000101001X")
        self.assertEqual(read_photo_zip_members(content), {filename: JPEG})

    def test_legacy_database_text_can_be_repaired_for_existing_preview(self):
        garbled = "张三_32050020000101001X.jpg".encode("gbk").decode("cp437")
        self.assertEqual(
            repair_legacy_zip_text(garbled),
            "张三_32050020000101001X.jpg",
        )

    def test_invalid_filename_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_photo_filename("没有身份证号.jpg")

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            inspect_photo_zip(make_zip({"../张三_32050020000101001X.jpg": JPEG}))

    def test_extension_and_content_must_match(self):
        with self.assertRaises(ValueError):
            inspect_photo_zip(make_zip({"张三_32050020000101001X.png": JPEG}))

    def test_invalid_identity_is_kept_for_unmatched_review(self):
        parsed = inspect_photo_zip(make_zip({"张三_身份证待核对.jpg": JPEG}))
        self.assertEqual(parsed[0].identity_number, "")
        self.assertTrue(parsed[0].parse_error)
        self.assertEqual(parsed[0].person_name, "张三")

    def test_duplicate_safe_names_are_rejected(self):
        with self.assertRaises(ValueError):
            inspect_photo_zip(make_zip({
                "一组/张三_32050020000101001X.jpg": JPEG,
                "二组/张三_32050020000101001X.jpg": JPEG + b"different",
            }))


class PhotoImportPermissionTests(unittest.TestCase):
    def test_only_base_control_or_workflow_manager_can_upload(self):
        self.assertTrue(_can_upload_photo_batch({
            "permissions": ["workflow.ticket.handle"],
            "member": {"position": "基础管控"},
        }))
        self.assertTrue(_can_upload_photo_batch({
            "permissions": ["workflow.ticket.manage"],
            "member": {"position": "管理员"},
        }))
        self.assertFalse(_can_upload_photo_batch({
            "permissions": ["workflow.ticket.handle"],
            "member": {"position": "组员"},
        }))

    def test_photo_workbench_shows_claimable_and_current_users_claimed_tickets(self):
        queue = _photo_pending_queue({
            "id": 17,
            "permissions": ["workflow.ticket.handle"],
            "member": {"position": "基础管控"},
        })
        where, params = _photo_pending_filter(queue, 17, True)
        clause = " ".join(where)

        self.assertEqual(queue, "基础管控")
        self.assertIn("order_row.status='queued'", clause)
        self.assertIn("order_row.status='in_progress'", clause)
        self.assertIn("order_row.current_assignee_user_id=%s", clause)
        self.assertEqual(params, ["基础管控", 17])

    def test_batch_claim_filter_only_includes_unassigned_queued_tickets(self):
        where, params = _photo_pending_filter("基础管控")
        clause = " ".join(where)

        self.assertIn("order_row.status='queued'", clause)
        self.assertIn("order_row.current_assignee_user_id IS NULL", clause)
        self.assertNotIn("order_row.status='in_progress'", clause)
        self.assertEqual(params, ["基础管控"])

    def test_sensitive_photo_filters_are_post_body_only(self):
        expected_models = {
            "/api/workflow/photo-requests/pending/search": PhotoRequestSearchPayload,
            "/api/workflow/photo-requests/pending/export": PhotoRequestFilterPayload,
        }

        for path, expected_model in expected_models.items():
            route = next(item for item in router.routes if item.path == path)
            self.assertEqual(route.methods, {"POST"})
            self.assertEqual(route.dependant.query_params, [])
            self.assertEqual(len(route.dependant.body_params), 1)
            self.assertIs(route.dependant.body_params[0].type_, expected_model)


class PhotoImportMatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_requires_in_progress_ticket_and_checks_each_attachment(self):
        cursor = type("Cursor", (), {})()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[
            (11, "PHOTO-11", 101, "张三", "in_progress"),
            (12, "PHOTO-12", 102, "张三", "in_progress"),
        ])
        cursor.fetchone = AsyncMock(side_effect=[(1,), None])

        rows, duplicate_all = await _photo_matches(cursor, "hmac", "sha")

        self.assertEqual([row[0] for row in rows], [11, 12])
        self.assertFalse(duplicate_all)
        self.assertEqual(cursor.fetchone.await_count, 2)

    async def test_match_accepts_recent_external_batch_completed_ticket_without_attachment(self):
        cursor = type("Cursor", (), {})()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[
            (887, "PHOTO-887", 12, "吕强", "completed"),
        ])
        cursor.fetchone = AsyncMock(return_value=None)

        rows, duplicate_all = await _photo_matches(cursor, "hmac", "sha")

        self.assertEqual([row[0] for row in rows], [887])
        self.assertFalse(duplicate_all)
        self.assertIn("external_batch_complete", cursor.execute.call_args_list[0].args[0])
        self.assertIn("INTERVAL 7 DAY", cursor.execute.call_args_list[0].args[0])

    async def test_cached_completed_ticket_can_check_multiple_photo_hashes(self):
        cursor = type("Cursor", (), {})()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(side_effect=[None, None])
        rows = [(887, "PHOTO-887", 12, "张三", "completed")]

        first_duplicate = await _photo_duplicate_all(cursor, rows, "sha-1")
        second_duplicate = await _photo_duplicate_all(cursor, rows, "sha-2")

        self.assertFalse(first_duplicate)
        self.assertFalse(second_duplicate)
        self.assertEqual(cursor.execute.await_count, 2)

    async def test_refresh_existing_partial_batch_recomputes_ticket_matches(self):
        item = inspect_photo_zip(make_zip({"张三_32050020000101001X.jpg": JPEG}))[0]
        cursor = type("Cursor", (), {})()
        cursor.execute = AsyncMock()
        with patch(
            "routers.workflow_extended._photo_matches",
            new=AsyncMock(return_value=([(887, "PHOTO-887", 12, "张三", "completed")], False)),
        ), patch("routers.workflow_extended.hmac_digest", return_value=("hmac", 1)):
            counts = await _refresh_photo_import_preview(cursor, 3, [item])

        self.assertEqual(counts["matched"], 1)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("UPDATE photo_request_import_items" in item for item in statements))
        self.assertTrue(any("UPDATE photo_request_import_batches" in item for item in statements))


class PhotoRequestBatchClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_claim_moves_all_claimable_tickets_into_progress(self):
        class Cursor:
            def __init__(self):
                self.statements = []
                self.rowcount = 1
                self.steps = [(201,), (202,)]

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def execute(self, statement, params=()):
                self.statements.append((statement, params))

            async def fetchall(self):
                return [(11, 101), (12, 102)]

            async def fetchone(self):
                return self.steps.pop(0)

        cursor = Cursor()
        conn = type("Conn", (), {})()
        conn.begin = AsyncMock()
        conn.commit = AsyncMock()
        conn.rollback = AsyncMock()
        conn.cursor = MagicMock(return_value=cursor)
        user = {
            "id": 17,
            "permissions": ["workflow.ticket.handle"],
            "member": {"position": "基础管控"},
        }

        with patch("routers.workflow_extended.workflow_notification", new=AsyncMock()) as notify:
            with patch("routers.workflow_extended.record_admin_audit", new=AsyncMock()):
                result = await batch_claim_photo_requests(
                    PhotoRequestBatchClaimPayload(claim_all=True),
                    MagicMock(),
                    user,
                    conn,
                )

        self.assertEqual(result["claimed_ids"], [11, 12])
        self.assertEqual(result["claimed_count"], 2)
        self.assertEqual(result["skipped_ids"], [])
        self.assertEqual(notify.await_count, 2)
        conn.commit.assert_awaited_once()
        conn.rollback.assert_not_awaited()
        statements = [statement for statement, _ in cursor.statements]
        self.assertEqual(sum("UPDATE work_orders SET current_assignee_user_id" in item for item in statements), 2)
        self.assertEqual(sum("INSERT INTO work_order_events" in item for item in statements), 2)


class RestoreQueuedTicketTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_can_restore_pending_requester_ticket_to_queue(self):
        class Cursor:
            def __init__(self):
                self.statements = []
                self.rowcount = 1
                self.results = [
                    ("pending_requester", "基础管控", 4, 14),
                    (201, "基础管控", 24),
                ]

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def execute(self, statement, params=()):
                self.statements.append((statement, params))

            async def fetchone(self):
                return self.results.pop(0)

            async def fetchall(self):
                return [(31,), (32,)]

        cursor = Cursor()
        conn = type("Conn", (), {})()
        conn.begin = AsyncMock()
        conn.commit = AsyncMock()
        conn.rollback = AsyncMock()
        conn.cursor = MagicMock(return_value=cursor)
        user = {"id": 3, "role": "admin", "permissions": []}

        with patch("routers.workflow_extended.workflow_notification", new=AsyncMock()) as notify, \
             patch("routers.workflow_extended.queue_user_ids", new=AsyncMock(return_value=[31, 32])), \
             patch("routers.workflow_extended.record_admin_audit", new=AsyncMock()) as audit, \
             patch("routers.workflow_extended.request_audit_fields", return_value={}):
            result = await restore_ticket_to_queue(
                11880,
                RestoreQueuedPayload(expected_version=4, reason="误操作恢复"),
                MagicMock(),
                user,
                conn,
            )

        self.assertEqual(result["status"], "queued")
        conn.commit.assert_awaited_once()
        conn.rollback.assert_not_awaited()
        statements = [statement for statement, _ in cursor.statements]
        self.assertTrue(any("FROM work_orders WHERE id=%s FOR UPDATE" in item for item in statements))
        self.assertTrue(any("SET status='queued', current_assignee_user_id=NULL" in item for item in statements))
        self.assertFalse(any("decision_note=''" in item for item in statements))
        self.assertTrue(any("event_type,actor_user_id" in item and "restore_queued" in item for item in statements))
        notify.assert_awaited_once()
        audit.assert_awaited_once()

    async def test_non_admin_cannot_restore_pending_requester_ticket(self):
        conn = MagicMock()

        with self.assertRaisesRegex(Exception, "只有管理员或超级管理员"):
            await restore_ticket_to_queue(
                11880,
                RestoreQueuedPayload(expected_version=4, reason="误操作恢复"),
                MagicMock(),
                {"id": 17, "role": "member", "permissions": ["workflow.ticket.handle"]},
                conn,
            )

        conn.begin.assert_not_called()


class _PreviewCursor:
    def __init__(self):
        self.execute = AsyncMock()
        self.fetchone = AsyncMock(return_value=None)
        self.fetchall = AsyncMock(return_value=[])
        self.lastrowid = 42

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _PreviewConnection:
    def __init__(self):
        self.cursor_obj = _PreviewCursor()
        self.begin = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    def cursor(self):
        return self.cursor_obj


class _DetailCursor:
    def __init__(self, garbled_name: str, garbled_person: str):
        self.execute = AsyncMock()
        self.fetchone = AsyncMock(return_value=(
            42, "PHOTO-42", "preview", 1, 0, 1, 0, 0, 0, "",
            None, None, None, None,
        ))
        self.fetchall = AsyncMock(return_value=[(
            garbled_name, garbled_person, "hmac", len(JPEG), "sha",
            "unmatched", "没有处理中照片工单", "[]",
        )])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _DetailConnection:
    def __init__(self, garbled_name: str, garbled_person: str):
        self.cursor_obj = _DetailCursor(garbled_name, garbled_person)

    def cursor(self):
        return self.cursor_obj


class _Upload:
    filename = "photos.zip"

    def __init__(self, content: bytes):
        self.content = content

    async def read(self, _limit: int) -> bytes:
        return self.content


class PhotoImportPreviewRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_detail_returns_repaired_full_identity(self):
        filename = "张三_32050020000101001X.jpg"
        connection = _DetailConnection(
            filename.encode("gbk").decode("cp437"),
            "张三".encode("gbk").decode("cp437"),
        )

        result = await get_photo_import_detail(42, {"id": 7}, connection)

        self.assertEqual(result["items"][0]["safe_name"], filename)
        self.assertEqual(result["items"][0]["person_name"], "张三")
        self.assertEqual(
            result["items"][0]["identity_number"],
            "32050020000101001X",
        )
        self.assertNotIn("identity_masked", result["items"][0])

    async def test_preview_only_creates_preview_rows_and_not_attachments(self):
        content = make_zip({"张三_32050020000101001X.jpg": JPEG})
        connection = _PreviewConnection()
        request = MagicMock()
        user = {
            "id": 7,
            "role": "admin",
            "permissions": ["workflow.ticket.handle"],
            "member": {"position": "基础管控"},
        }
        with patch("routers.workflow_extended.record_admin_audit", new=AsyncMock()), \
             patch("routers.workflow_extended.request_audit_fields", return_value={}), \
             patch("routers.workflow_extended.save_photo_import_zip", return_value="42.zip"), \
             patch("routers.workflow_extended.get_photo_import_detail", new=AsyncMock(return_value={"id": 42})), \
             patch("routers.workflow_extended.hmac_digest", return_value=("hmac", 1)):
            result = await preview_photo_import(request, _Upload(content), user, connection)

        self.assertEqual(result["id"], 42)
        self.assertTrue(connection.commit.await_count)
        statements = [call.args[0] for call in connection.cursor_obj.execute.call_args_list]
        self.assertTrue(any("photo_request_import_items" in statement for statement in statements))
        self.assertFalse(any("INSERT INTO work_order_attachments" in statement for statement in statements))

    async def test_reupload_restores_missing_existing_preview_zip(self):
        content = make_zip({"张三_32050020000101001X.jpg": JPEG})
        connection = _PreviewConnection()
        connection.cursor_obj.fetchone = AsyncMock(return_value=(
            9,
            "PHOTO-OLD",
            "preview",
            "existing-token.zip",
        ))
        request = MagicMock()
        user = {
            "id": 7,
            "role": "admin",
            "permissions": ["workflow.ticket.handle"],
            "member": {"position": "基础管控"},
        }
        audit = AsyncMock()
        with patch("routers.workflow_extended.record_admin_audit", new=audit), \
             patch("routers.workflow_extended.request_audit_fields", return_value={}), \
             patch("routers.workflow_extended.resolve_photo_import_zip", side_effect=FileNotFoundError), \
             patch("routers.workflow_extended.save_photo_import_zip", return_value="existing-token.zip") as save_zip, \
             patch("routers.workflow_extended.get_photo_import_detail", new=AsyncMock(return_value={"id": 9})):
            result = await preview_photo_import(request, _Upload(content), user, connection)

        self.assertEqual(result["id"], 9)
        save_zip.assert_called_once_with("existing-token", content)
        connection.commit.assert_awaited_once()
        statements = [call.args[0] for call in connection.cursor_obj.execute.call_args_list]
        self.assertTrue(any("FOR UPDATE" in statement for statement in statements))
        self.assertTrue(any("SET storage_key=%s" in statement for statement in statements))
        self.assertEqual(
            audit.await_args.kwargs["detail"],
            {"batch_id": 9, "restored_preview_file": True},
        )

    async def test_reupload_keeps_existing_preview_when_zip_is_present(self):
        content = make_zip({"张三_32050020000101001X.jpg": JPEG})
        connection = _PreviewConnection()
        connection.cursor_obj.fetchone = AsyncMock(return_value=(
            9,
            "PHOTO-OLD",
            "preview",
            "existing-token.zip",
        ))
        request = MagicMock()
        user = {
            "id": 7,
            "role": "admin",
            "permissions": ["workflow.ticket.handle"],
            "member": {"position": "基础管控"},
        }
        with patch("routers.workflow_extended.record_admin_audit", new=AsyncMock()) as audit, \
             patch("routers.workflow_extended.resolve_photo_import_zip"), \
             patch("routers.workflow_extended.save_photo_import_zip") as save_zip, \
             patch("routers.workflow_extended.get_photo_import_detail", new=AsyncMock(return_value={"id": 9})):
            result = await preview_photo_import(request, _Upload(content), user, connection)

        self.assertEqual(result["id"], 9)
        save_zip.assert_not_called()
        connection.rollback.assert_awaited_once()
        connection.commit.assert_not_awaited()
        audit.assert_not_awaited()

    async def test_reupload_partial_batch_refreshes_matches_and_returns_to_preview(self):
        content = make_zip({"张三_32050020000101001X.jpg": JPEG})
        connection = _PreviewConnection()
        connection.cursor_obj.fetchone = AsyncMock(return_value=(
            9,
            "PHOTO-OLD",
            "partial",
            "existing-token.zip",
        ))
        request = MagicMock()
        user = {
            "id": 7,
            "role": "admin",
            "permissions": ["workflow.ticket.handle"],
            "member": {"position": "基础管控"},
        }
        with patch("routers.workflow_extended.record_admin_audit", new=AsyncMock()), \
             patch("routers.workflow_extended.request_audit_fields", return_value={}), \
             patch("routers.workflow_extended.save_photo_import_zip", return_value="existing-token.zip") as save_zip, \
             patch("routers.workflow_extended._refresh_photo_import_preview", new=AsyncMock(return_value={"matched": 1})) as refresh, \
             patch("routers.workflow_extended.get_photo_import_detail", new=AsyncMock(return_value={"id": 9})):
            result = await preview_photo_import(request, _Upload(content), user, connection)

        self.assertEqual(result["id"], 9)
        save_zip.assert_called_once_with("existing-token", content)
        refresh.assert_awaited_once()
        connection.commit.assert_awaited_once()
        statements = [call.args[0] for call in connection.cursor_obj.execute.call_args_list]
        self.assertTrue(any("status='preview'" in item for item in statements))


class PhotoImportConfirmRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_uses_each_matched_ticket_subject_name(self):
        identity = "32050020000101001X"
        content = make_zip({f"Uploaded Name_{identity}.jpg": JPEG})
        zip_sha256 = hashlib.sha256(content).hexdigest()

        class Cursor:
            def __init__(self):
                self.statement = ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def execute(self, statement, _params=()):
                self.statement = statement

            async def fetchone(self):
                if "SELECT storage_key" in self.statement:
                    return ("batch.zip", zip_sha256, "preview", "PHOTO-TEST")
                if "SELECT status, batch_no" in self.statement:
                    return ("preview", "PHOTO-TEST")
                if "SELECT COUNT(*) FROM work_order_attachments" in self.statement:
                    return (0,)
                if "SELECT requester_user_id" in self.statement:
                    return (101, 1, "in_progress", 7, "base_control")
                if "SELECT external_origin" in self.statement:
                    return ("manual",)
                return None

            async def fetchall(self):
                if "FROM work_orders order_row" in self.statement:
                    return [(11, "PHOTO-11", 101, "Ticket Subject", "in_progress")]
                return []

        cursor = Cursor()
        conn = type("Conn", (), {})()
        conn.begin = AsyncMock()
        conn.commit = AsyncMock()
        conn.rollback = AsyncMock()
        conn.cursor = MagicMock(return_value=cursor)
        path = MagicMock()
        path.read_bytes.return_value = content
        saved = {
            "file_id": "file-id",
            "storage_key": "11/file-id.jpg",
            "mime_type": "image/jpeg",
            "sha256": hashlib.sha256(JPEG).hexdigest(),
            "size_bytes": len(JPEG),
        }
        user = {"id": 7, "role": "super_admin", "permissions": []}

        with patch("routers.workflow_extended.resolve_photo_import_zip", return_value=path), \
             patch("routers.workflow_extended.hmac_digest", return_value=("hmac", 1)), \
             patch("routers.workflow_extended.save_attachment", return_value=saved) as save, \
             patch("routers.workflow_extended.remove_photo_import_zip"), \
             patch("routers.workflow_extended.workflow_notification", new=AsyncMock()), \
             patch("routers.workflow_extended.record_admin_audit", new=AsyncMock()), \
             patch("routers.workflow_extended.request_audit_fields", return_value={}), \
             patch("routers.workflow_extended.get_photo_import_detail", new=AsyncMock(return_value={"id": 42})):
            result = await confirm_photo_import(42, MagicMock(), user, conn)

        self.assertEqual(result, {"id": 42})
        self.assertEqual(save.call_args.args[1], f"Ticket Subject_{identity}.jpg")
        conn.commit.assert_awaited_once()
        conn.rollback.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
