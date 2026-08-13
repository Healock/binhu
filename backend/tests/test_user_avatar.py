import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException, UploadFile

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.auth import _remove_avatar, _resolve_avatar, get_avatar, upload_avatar


JPEG = b"\xff\xd8\xffavatar"


class AvatarCursor:
    def __init__(self, previous_storage_key=None, fail_update=False):
        self.previous_storage_key = previous_storage_key
        self.fail_update = fail_update
        self.last_sql = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        del params
        self.last_sql = " ".join(sql.split())
        if self.fail_update and self.last_sql.startswith("UPDATE _users"):
            raise RuntimeError("database unavailable")

    async def fetchone(self):
        if self.last_sql.startswith("SELECT avatar_storage_key FROM _users"):
            return (self.previous_storage_key,)
        if self.last_sql.startswith("SELECT avatar_storage_key, avatar_mime"):
            return (self.previous_storage_key, "image/jpeg")
        return None


class AvatarPool:
    def __init__(self, cursor):
        self.connection = MagicMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=cursor)
        context.__aexit__ = AsyncMock(return_value=None)
        self.connection.cursor.return_value = context
        self.acquire = AsyncMock(return_value=self.connection)
        self.release = MagicMock()


class UserAvatarTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_replaces_old_file_and_returns_versioned_url(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "7" / "old.jpg"
            old_path.parent.mkdir(parents=True)
            old_path.write_bytes(JPEG)
            pool = AvatarPool(AvatarCursor("7/old.jpg"))
            upload = UploadFile(filename="avatar.jpg", file=io.BytesIO(JPEG))
            with patch("routers.auth.settings.USER_AVATAR_DIR", directory), \
                 patch("routers.auth.db_manager.get_pool", return_value=pool):
                result = await upload_avatar(upload, user={"id": 7})
            self.assertFalse(old_path.exists())
            self.assertIn("/api/auth/avatar/7?v=", result["avatar_url"])
            self.assertEqual(len(list((root / "7").glob("*.jpg"))), 1)

    async def test_database_failure_removes_new_file_and_keeps_old_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "7" / "old.jpg"
            old_path.parent.mkdir(parents=True)
            old_path.write_bytes(JPEG)
            pool = AvatarPool(AvatarCursor("7/old.jpg", fail_update=True))
            upload = UploadFile(filename="avatar.jpg", file=io.BytesIO(JPEG))
            with patch("routers.auth.settings.USER_AVATAR_DIR", directory), \
                 patch("routers.auth.db_manager.get_pool", return_value=pool):
                with self.assertRaises(RuntimeError):
                    await upload_avatar(upload, user={"id": 7})
            self.assertTrue(old_path.exists())
            self.assertEqual(list((root / "7").glob("*.jpg")), [old_path])

    async def test_avatar_download_is_private_and_not_cached(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "7" / "avatar.jpg"
            path.parent.mkdir(parents=True)
            path.write_bytes(JPEG)
            pool = AvatarPool(AvatarCursor("7/avatar.jpg"))
            with patch("routers.auth.settings.USER_AVATAR_DIR", directory), \
                 patch("routers.auth.db_manager.get_pool", return_value=pool):
                response = await get_avatar(7, _user={"id": 7})
            self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_avatar_path_cannot_escape_configured_root(self):
        with TemporaryDirectory() as directory:
            outside = Path(directory).parent / "outside-avatar.jpg"
            outside.write_bytes(JPEG)
            try:
                with patch("routers.auth.settings.USER_AVATAR_DIR", directory):
                    with self.assertRaises(FileNotFoundError):
                        _resolve_avatar(f"../{outside.name}")
                    _remove_avatar(f"../{outside.name}")
                self.assertTrue(outside.exists())
            finally:
                outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
