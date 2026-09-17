import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.query import (
    SourceRowBulkCreate,
    SourceRowBulkItem,
    _create_local_source_rows_bulk,
    create_source_rows_bulk,
)


class FakeParser:
    COLUMNS = ["社区", "身份证号"]
    COMMUNITY_COLUMN = "社区"
    table_name = "t_fixture"

    def get_business_key(self):
        return ["身份证号"]

    def validate_new_row(self, values):
        if not values["社区"]:
            raise ValueError("社区不能为空")

    def make_row_key(self, values):
        return f"key-{values['身份证号']}"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = 0
        self._one = None
        self._all = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.conn.statements.append((compact, params))
        self._one = None
        self._all = []
        if compact.startswith("SELECT GET_LOCK"):
            self._one = (1,)
        elif compact.startswith("SELECT RELEASE_LOCK"):
            self._one = (1,)
        elif compact.startswith("SELECT row_key FROM _online_source_rows"):
            self._all = [(key,) for key in self.conn.existing]
        elif compact.startswith("INSERT INTO `t_fixture`"):
            self.conn.next_id += 1
            self.lastrowid = self.conn.next_id
        elif compact.startswith("SELECT id, revision FROM _online_source_rows"):
            self.conn.next_source_id += 1
            self._one = (self.conn.next_source_id, 1)
        elif compact.startswith("INSERT INTO _admin_audit_log"):
            self.lastrowid = 1

    async def fetchone(self):
        return self._one

    async def fetchall(self):
        return self._all


class FakeConnection:
    def __init__(self, *, existing=()):
        self.existing = list(existing)
        self.statements = []
        self.next_id = 10
        self.next_source_id = 100
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self):
        return FakeCursor(self)

    async def begin(self):
        self.begin_count += 1

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def request():
    value = Mock()
    value.headers = {}
    value.client = None
    return value


class QueryBulkCreateTests(unittest.IsolatedAsyncioTestCase):
    async def call_helper(self, items, conn, *, projection=None):
        projection = projection or AsyncMock()
        with patch("routers.query.validate_new_row_scope", AsyncMock(return_value=None)), \
             patch("routers.query.rebuild_projection_rows", projection), \
             patch("routers.query.enqueue_event", AsyncMock()), \
             patch("routers.query.record_admin_audit", AsyncMock()):
            return await _create_local_source_rows_bulk(
                parser_type="fixture",
                parser=FakeParser(),
                items=items,
                request=request(),
                user={"id": 1, "username": "tester"},
                conn=conn,
            )

    async def test_success_preserves_request_order_and_commits_once(self):
        conn = FakeConnection()
        rows = await self.call_helper([
            SourceRowBulkItem(draft_id="d2", values={"社区": "甲", "身份证号": "2"}),
            SourceRowBulkItem(draft_id="d1", values={"社区": "甲", "身份证号": "1"}),
        ], conn)
        self.assertEqual([row["draft_id"] for row in rows], ["d2", "d1"])
        self.assertEqual([row["row_key"] for row in rows], ["key-2", "key-1"])
        self.assertEqual(conn.begin_count, 1)
        self.assertEqual(conn.commit_count, 1)
        self.assertEqual(conn.rollback_count, 0)
        self.assertTrue(any(sql.startswith("SELECT RELEASE_LOCK") for sql, _ in conn.statements))

    async def test_validation_collects_all_rows_without_starting_transaction(self):
        conn = FakeConnection(existing=("key-9",))
        with self.assertRaises(HTTPException) as raised:
            await self.call_helper([
                SourceRowBulkItem(draft_id="missing", values={"社区": "甲", "身份证号": ""}),
                SourceRowBulkItem(draft_id="existing", values={"社区": "甲", "身份证号": "9"}),
            ], conn)
        detail = raised.exception.detail
        self.assertEqual([error["code"] for error in detail["errors"]], [
            "missing_required", "duplicate_existing",
        ])
        self.assertEqual(conn.begin_count, 0)
        self.assertEqual(conn.commit_count, 0)

    async def test_duplicate_request_rolls_back_before_writes(self):
        conn = FakeConnection()
        with self.assertRaises(HTTPException) as raised:
            await self.call_helper([
                SourceRowBulkItem(draft_id="first", values={"社区": "甲", "身份证号": "8"}),
                SourceRowBulkItem(draft_id="second", values={"社区": "甲", "身份证号": "8"}),
            ], conn)
        self.assertEqual(raised.exception.detail["errors"][0]["code"], "duplicate_request")
        self.assertEqual(conn.begin_count, 0)

    async def test_projection_failure_rolls_back_entire_batch_and_releases_lock(self):
        conn = FakeConnection()
        projection = AsyncMock(side_effect=RuntimeError("projection failed"))
        with self.assertRaisesRegex(RuntimeError, "projection failed"):
            await self.call_helper([
                SourceRowBulkItem(draft_id="one", values={"社区": "甲", "身份证号": "1"}),
            ], conn, projection=projection)
        self.assertEqual(conn.commit_count, 0)
        self.assertEqual(conn.rollback_count, 1)
        self.assertTrue(any(sql.startswith("SELECT RELEASE_LOCK") for sql, _ in conn.statements))

    async def test_non_local_mode_stops_before_bulk_service(self):
        data = SourceRowBulkCreate(rows=[
            SourceRowBulkItem(draft_id="one", values={"社区": "甲", "身份证号": "1"}),
        ])
        with patch("routers.query.local_data_source_enabled", return_value=False), \
             patch("routers.query._create_local_source_rows_bulk", AsyncMock()) as service:
            with self.assertRaises(HTTPException) as raised:
                await create_source_rows_bulk(
                    "疑似返苏", data, request(), user={"id": 1}, conn=FakeConnection(),
                )
        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(raised.exception.detail, "腾讯数据源已下线")
        service.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
