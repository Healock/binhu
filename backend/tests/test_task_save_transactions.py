import inspect
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pymysql.err import OperationalError

from routers import query


class _Connection:
    def __init__(self):
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1


class LocalTaskSaveRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadlock_retries_complete_transaction(self):
        conn = _Connection()
        outcome = {"revision": 2}
        inner = AsyncMock(side_effect=[OperationalError(1213, "deadlock"), outcome])
        with (
            patch.object(query, "_update_local_source_fields_once", inner),
            patch.object(query.asyncio, "sleep", AsyncMock()),
        ):
            result = await query._update_local_source_fields(conn=conn)
        self.assertEqual(result, outcome)
        self.assertEqual(inner.await_count, 2)
        self.assertEqual(conn.rollbacks, 1)
        self.assertEqual(len({call.kwargs["operation_id"] for call in inner.await_args_list}), 1)

    async def test_three_deadlocks_return_busy_not_conflict(self):
        conn = _Connection()
        inner = AsyncMock(side_effect=OperationalError(1213, "deadlock"))
        with (
            patch.object(query, "_update_local_source_fields_once", inner),
            patch.object(query.asyncio, "sleep", AsyncMock()),
        ):
            with self.assertRaises(HTTPException) as raised:
                await query._update_local_source_fields(conn=conn)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "task_save_busy")
        self.assertTrue(raised.exception.detail["operation_id"])
        self.assertEqual(conn.rollbacks, 3)

    async def test_lock_timeout_returns_structured_timeout(self):
        conn = _Connection()
        inner = AsyncMock(side_effect=OperationalError(1205, "timeout"))
        with (
            patch.object(query, "_update_local_source_fields_once", inner),
            patch.object(query.asyncio, "sleep", AsyncMock()),
        ):
            with self.assertRaises(HTTPException) as raised:
                await query._update_local_source_fields(conn=conn)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "task_save_timeout")

    def test_request_transaction_has_fixed_lock_order_and_no_heavy_derivations(self):
        source = inspect.getsource(query._update_local_source_fields_once)
        source_lock = source.index("source = await _load_source_row")
        local_lock = source.index("SELECT id FROM _local_source_records")
        business_lock = source.index("WHERE _row_key=%s FOR UPDATE")
        self.assertLess(source_lock, local_lock)
        self.assertLess(local_lock, business_lock)
        self.assertNotIn("await rebuild_projection_rows", source)
        self.assertNotIn("await reconcile_online_task_graph", source)
        self.assertIn("await enqueue_projection_jobs", source)

    def test_non_conflicting_stale_revision_advances_from_locked_revision(self):
        source = inspect.getsource(query._update_local_source_fields_once)
        self.assertIn('locked_revision = int(source["revision"])', source)
        self.assertIn('"current_revision": locked_revision', source)
        self.assertIn('"code": "task_revision_conflict"', source)
        self.assertIn('"revision": locked_revision + 1', source)
        self.assertNotIn('"revision": expected_revision + 1', source)

    def test_post_commit_ledgers_cannot_reenter_transaction_retry(self):
        source = inspect.getsource(query._update_local_source_fields_once)
        commit_position = source.index("await conn.commit()")
        audit_position = source.index("await record_admin_audit(")
        activity_position = source.index("await record_work_activity(")
        self.assertLess(commit_position, audit_position)
        self.assertLess(commit_position, activity_position)
        self.assertIn("post-commit admin audit failed", source)
        self.assertIn("post-commit work activity failed", source)


if __name__ == "__main__":
    unittest.main()
