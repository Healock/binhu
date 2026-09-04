import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

import database


class DatabasePoolTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_db_releases_an_acquired_connection(self):
        connection = object()
        pool = MagicMock()
        async def acquire():
            return connection
        pool.acquire.side_effect = acquire
        pool.release = MagicMock()
        generator = database.get_db()
        async def wait_for(awaitable, timeout):
            awaitable.close()
            return connection
        with (
            patch.object(database.db_manager, "get_pool", return_value=pool),
            patch.object(database.asyncio, "wait_for", side_effect=wait_for),
        ):
            yielded = await anext(generator)
            self.assertIs(yielded, connection)
            await generator.aclose()
        pool.release.assert_called_once_with(connection)

    async def test_get_db_returns_structured_503_when_pool_is_busy(self):
        pool = MagicMock()
        async def acquire():
            return object()
        pool.acquire.side_effect = acquire
        generator = database.get_db()
        async def wait_for(awaitable, timeout):
            awaitable.close()
            raise TimeoutError
        with (
            patch.object(database.db_manager, "get_pool", return_value=pool),
            patch.object(database.asyncio, "wait_for", side_effect=wait_for),
        ):
            with self.assertRaises(HTTPException) as raised:
                await anext(generator)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "database_pool_busy")
        pool.release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
