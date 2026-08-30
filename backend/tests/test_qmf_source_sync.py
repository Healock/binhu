import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.qmf_source_sync import _sync_rows


class _Cursor:
    def __init__(self):
        self.executed: list[str] = []
        self.rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.executed.append(normalized)
        if normalized.startswith("SELECT _row_key,"):
            self.rows = []
        elif normalized.startswith("SELECT id,row_key,physical_row,values_json"):
            self.rows = []
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    async def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self):
        self.db = b"OnlineData"
        self.cursor_instance = _Cursor()
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


class _Pool:
    def __init__(self, connection):
        self.connection = connection
        self.released = False

    async def acquire(self):
        return self.connection

    def release(self, connection):
        self.released = connection is self.connection


class QmfSourceSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_source_reads_existing_business_rows_by_internal_row_key(self):
        connection = _Connection()
        pool = _Pool(connection)
        ctx = type("Context", (), {"update": AsyncMock()})()
        column_map = {
            "截止时间": "截止时间",
            "核查人": "核查人",
            "姓名": "姓名",
            "身份证号": "身份证号",
            "联系方式": "联系方式",
            "地址": "地址",
            "下发社区": "下发社区",
            "核查结果": "核查结果",
            "备注": "备注",
        }

        with (
            patch("services.qmf_source_sync.db_manager.get_pool", return_value=pool),
            patch(
                "services.qmf_source_sync.get_database_column_map",
                AsyncMock(return_value=column_map),
            ),
            patch("services.qmf_source_sync.rebuild_projection", AsyncMock()) as rebuild,
            patch(
                "services.qmf_source_sync.apply_self_owned_matches",
                AsyncMock(return_value={
                    "matched_tasks": 2,
                    "updated_tasks": 1,
                    "skipped_tasks": 1,
                }),
            ) as self_owned,
        ):
            result = await _sync_rows(
                ctx,
                {
                    "rows": [],
                    "record_count": 0,
                    "unresolved_count": 0,
                    "issue_count": 0,
                },
            )

        self.assertEqual(result["status"], "success")
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertTrue(pool.released)
        self.assertTrue(connection.cursor_instance.executed[0].startswith("SELECT _row_key,"))
        self.assertEqual(rebuild.await_count, 2)
        self_owned.assert_awaited_once()
        self.assertEqual(result["self_owned_matched"], 2)
        self.assertEqual(result["self_owned_updated"], 1)
        self.assertFalse(
            any("daily_report" in sql for sql in connection.cursor_instance.executed)
        )


if __name__ == "__main__":
    unittest.main()
