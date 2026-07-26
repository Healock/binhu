from datetime import date
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.sync_engine import SyncEngine


class SyncSnapshotTimezoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_uses_business_date(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor_context = MagicMock()
        cursor_context.__aenter__ = AsyncMock(return_value=cursor)
        cursor_context.__aexit__ = AsyncMock(return_value=None)
        connection = MagicMock()
        connection.cursor.return_value = cursor_context

        with patch(
            "services.sync_engine.get_business_date",
            new=AsyncMock(return_value=date(2026, 7, 27)),
        ):
            await SyncEngine(None)._save_snapshot(
                connection,
                "t_fullchain",
                "全链条",
            )

        executed_sql = [call.args[0] for call in cursor.execute.await_args_list]
        self.assertTrue(
            any("2026-07-27_snapshot_fullChain" in sql for sql in executed_sql)
        )


if __name__ == "__main__":
    unittest.main()
