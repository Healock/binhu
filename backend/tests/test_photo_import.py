import io
import unittest
import zipfile
from unittest.mock import AsyncMock
from unittest.mock import MagicMock, patch

from routers.workflow_extended import _can_upload_photo_batch, _photo_matches
from routers.workflow_extended import preview_photo_import
from services.photo_import import (
    inspect_photo_zip,
    parse_photo_filename,
)


JPEG = b"\xff\xd8\xffphoto"
PNG = b"\x89PNG\r\n\x1a\nphoto"


def make_zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


class PhotoImportParserTests(unittest.TestCase):
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


class _Upload:
    filename = "photos.zip"

    def __init__(self, content: bytes):
        self.content = content

    async def read(self, _limit: int) -> bytes:
        return self.content


class PhotoImportPreviewRouteTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertFalse(any("work_order_attachments" in statement for statement in statements))


if __name__ == "__main__":
    unittest.main()
