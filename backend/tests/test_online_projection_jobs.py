import unittest
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
