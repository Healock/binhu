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
            patch.object(online_projection_jobs, "_process_job", new=process),
        ):
            count = await online_projection_jobs.process_projection_jobs_once(limit=4)
        self.assertEqual(count, 4)
        self.assertEqual(peak, 4)

    def test_worker_concurrency_is_bounded_and_configurable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(online_projection_jobs._worker_concurrency(), 4)
        with patch.dict(os.environ, {"ONLINE_PROJECTION_WORKER_CONCURRENCY": "99"}):
            self.assertEqual(online_projection_jobs._worker_concurrency(), 8)
        with patch.dict(os.environ, {"ONLINE_PROJECTION_WORKER_CONCURRENCY": "0"}):
            self.assertEqual(online_projection_jobs._worker_concurrency(), 1)
        with patch.dict(os.environ, {"ONLINE_PROJECTION_WORKER_CONCURRENCY": "bad"}):
            self.assertEqual(online_projection_jobs._worker_concurrency(), 4)

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
            patch.object(online_projection_jobs, "rebuild_projection_keys", new=AsyncMock()) as rebuild,
            patch.object(online_projection_jobs, "reconcile_projection_task_graph_rows", new=AsyncMock()) as reconcile,
        ):
            await online_projection_jobs._process_job(job)
        connection.commit.assert_awaited_once()
        connection.rollback.assert_not_awaited()
        finish.assert_not_awaited()
        self.assertIn("status='skipped'", cursor.execute.await_args_list[-1].args[0])
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


if __name__ == "__main__":
    unittest.main()
