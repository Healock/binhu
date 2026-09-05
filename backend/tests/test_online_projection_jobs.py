import asyncio
import unittest
import os
from unittest.mock import AsyncMock, MagicMock, patch

from services import online_projection_jobs


class _Cursor:
    def __init__(self, row=(1,)):
        self.row = row
        self.rowcount = 1
        self.execute = AsyncMock()
        self.executemany = AsyncMock()

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return []


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return None


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.begin = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    def cursor(self):
        return _Context(self._cursor)


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Context(self.connection)


class ProjectionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_processes_claimed_batch_with_bounded_concurrency(self):
        jobs = [
            {"id": index, "parser_type": "全链条", "row_key": str(index),
             "source_id": index, "revision": 1, "operation_id": str(index),
             "attempt_count": 1}
            for index in range(4)
        ]
        active = 0
        peak = 0

        async def process(_job):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

        with (
            patch.object(online_projection_jobs, "_claim_jobs", new=AsyncMock(return_value=jobs)),
            patch.object(online_projection_jobs, "_process_batch", new=process),
            patch.dict(os.environ, {"ONLINE_PROJECTION_MICRO_BATCH_SIZE": "1"}),
        ):
            count = await online_projection_jobs.process_projection_jobs_once(limit=4)
        self.assertEqual(count, 4)
        self.assertEqual(peak, 3)

    def test_worker_concurrency_is_bounded_and_configurable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(online_projection_jobs._worker_concurrency(), 3)
        with patch.dict(os.environ, {"ONLINE_PROJECTION_WORKER_CONCURRENCY": "99"}):
            self.assertEqual(online_projection_jobs._worker_concurrency(), 3)
        with patch.dict(os.environ, {"ONLINE_PROJECTION_WORKER_CONCURRENCY": "0"}):
            self.assertEqual(online_projection_jobs._worker_concurrency(), 1)
        with patch.dict(os.environ, {"ONLINE_PROJECTION_WORKER_CONCURRENCY": "bad"}):
            self.assertEqual(online_projection_jobs._worker_concurrency(), 3)

    async def test_enqueue_is_revision_idempotent_without_task_body(self):
        cursor = _Cursor()
        count = await online_projection_jobs.enqueue_projection_jobs(
            cursor,
            parser_type="全链条",
            row_keys=["a" * 32, "a" * 32],
            source_id=9,
            revision=4,
            operation_id="operation-1",
        )
        self.assertEqual(count, 1)
        sql = cursor.executemany.await_args.args[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertNotIn("values_json", sql)
        self.assertEqual(len(cursor.executemany.await_args.args[1]), 1)

    async def test_stale_revision_is_skipped_before_derived_work(self):
        cursor = _Cursor(row=(5,))
        connection = _Connection(cursor)
        job = {
            "id": 1,
            "parser_type": "全链条",
            "row_key": "a" * 32,
            "source_id": 9,
            "revision": 4,
            "operation_id": "operation-1",
            "attempt_count": 1,
        }
        with (
            patch.object(online_projection_jobs, "_online_pool", return_value=_Pool(connection)),
            patch.object(online_projection_jobs, "_finish_job", new=AsyncMock()) as finish,
            patch.object(online_projection_jobs, "_current_revisions", new=AsyncMock(return_value={"a" * 32: 5})),
            patch.object(online_projection_jobs, "rebuild_projection_keys", new=AsyncMock()) as rebuild,
            patch.object(online_projection_jobs, "reconcile_projection_task_graph_rows", new=AsyncMock()) as reconcile,
        ):
            await online_projection_jobs._process_job(job)
        connection.commit.assert_awaited_once()
        connection.rollback.assert_not_awaited()
        finish.assert_not_awaited()
        self.assertTrue(any(
            "UPDATE _online_projection_jobs SET status=%s" in call.args[0]
            and call.args[1][0] == "skipped"
            for call in cursor.execute.await_args_list
        ))
        rebuild.assert_not_awaited()
        reconcile.assert_not_awaited()

    async def test_current_revision_rebuilds_and_reconciles_once(self):
        cursor = _Cursor(row=(4,))
        connection = _Connection(cursor)
        job = {
            "id": 1,
            "parser_type": "全链条",
            "row_key": "a" * 32,
            "source_id": 9,
            "revision": 4,
            "operation_id": "operation-1",
            "attempt_count": 1,
        }
        with (
            patch.object(online_projection_jobs, "_online_pool", return_value=_Pool(connection)),
            patch.object(online_projection_jobs, "rebuild_projection_keys", new=AsyncMock()) as rebuild,
            patch.object(online_projection_jobs, "reconcile_projection_task_graph_rows", new=AsyncMock()) as reconcile,
        ):
            await online_projection_jobs._process_job(job)
        rebuild.assert_awaited_once()
        reconcile.assert_awaited_once()
        connection.commit.assert_awaited_once()
        connection.rollback.assert_not_awaited()

    async def test_claimed_revisions_are_coalesced_before_derived_work(self):
        jobs = [
            {"id": revision, "parser_type": "全链条", "row_key": "a" * 32,
             "source_id": 9, "revision": revision, "operation_id": str(revision),
             "attempt_count": 1}
            for revision in range(1, 101)
        ]
        with (
            patch.object(online_projection_jobs, "_claim_jobs", new=AsyncMock(return_value=jobs)),
            patch.object(online_projection_jobs, "_finish_jobs", new=AsyncMock()) as finish,
            patch.object(online_projection_jobs, "_process_batch", new=AsyncMock()) as process,
        ):
            count = await online_projection_jobs.process_projection_jobs_once(limit=100)
        self.assertEqual(count, 100)
        self.assertEqual(len(finish.await_args.args[0]), 99)
        self.assertEqual(finish.await_args.args[1:], ("skipped", "coalesced_revision"))
        process.assert_awaited_once()
        self.assertEqual(process.await_args.args[0][0]["revision"], 100)

    def test_claim_and_micro_batch_sizes_are_bounded(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(online_projection_jobs._claim_limit(), 100)
            self.assertEqual(online_projection_jobs._micro_batch_size(), 25)
        with patch.dict(os.environ, {
            "ONLINE_PROJECTION_CLAIM_LIMIT": "999",
            "ONLINE_PROJECTION_MICRO_BATCH_SIZE": "999",
        }):
            self.assertEqual(online_projection_jobs._claim_limit(), 100)
            self.assertEqual(online_projection_jobs._micro_batch_size(), 50)

    async def test_missing_available_at_is_rechecked_after_short_ttl(self):
        first = _Cursor(row=(0,))
        online_projection_jobs._available_at_supported = None
        online_projection_jobs._available_at_checked_at = 0.0
        with patch.object(online_projection_jobs.time, "monotonic", return_value=10.0):
            self.assertFalse(await online_projection_jobs._queue_has_available_at(first))
        second = _Cursor(row=(1,))
        with patch.object(online_projection_jobs.time, "monotonic", return_value=16.0):
            self.assertTrue(await online_projection_jobs._queue_has_available_at(second))
        first.execute.assert_awaited_once()
        second.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
