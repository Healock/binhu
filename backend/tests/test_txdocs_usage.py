import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch


os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.txdocs_usage import _persist_usage_batch


class TxDocsUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_group_uses_the_full_attempt_count(self):
        cursor = MagicMock()
        cursor.executemany = AsyncMock()
        cursor_context = MagicMock()
        cursor_context.__aenter__ = AsyncMock(return_value=cursor)
        cursor_context.__aexit__ = AsyncMock(return_value=False)

        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=connection)

        bucket = datetime(2026, 8, 15, 10)
        events = [
            {
                "bucket_hour": bucket,
                "request_source": "sync",
                "endpoint": "range_read",
                "method": "GET",
                "success": 1,
                "failure": 0,
                "retry": 0,
                "quota_exhausted": 0,
                "http_status": 200,
                "error_code": "",
            },
            {
                "bucket_hour": bucket,
                "request_source": "sync",
                "endpoint": "range_read",
                "method": "GET",
                "success": 0,
                "failure": 1,
                "retry": 1,
                "quota_exhausted": 1,
                "http_status": 429,
                "error_code": "400011",
            },
        ]

        with patch("services.txdocs_usage.db_manager.get_pool", return_value=pool):
            await _persist_usage_batch(events)

        sql, values = cursor.executemany.await_args.args
        self.assertIn("attempt_count=attempt_count+VALUES(attempt_count)", sql)
        self.assertEqual(sql.count("%s"), 11)
        self.assertEqual(values, [(
            bucket,
            "sync",
            "range_read",
            "GET",
            2,
            1,
            1,
            1,
            1,
            429,
            "400011",
        )])
        pool.release.assert_called_once_with(connection)


if __name__ == "__main__":
    unittest.main()
