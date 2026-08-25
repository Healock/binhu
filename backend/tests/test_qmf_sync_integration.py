import unittest
from unittest.mock import AsyncMock, patch

from services.sync_engine import SyncEngine


class RecordingCursor:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))


class RecordingConnection:
    def __init__(self):
        self.cursor_instance = RecordingCursor()
        self.events = []

    def cursor(self):
        return self.cursor_instance

    async def begin(self):
        self.events.append("begin")

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


class QmfSyncIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_upsert_preserves_local_fields(self):
        conn = RecordingConnection()
        column_map = {
            column: column
            for column in (
                "截止时间",
                "核查人",
                "姓名",
                "身份证号",
                "联系方式",
                "地址",
                "下发社区",
                "核查结果",
                "备注",
            )
        }
        result = {
            "rows": [
                {
                    "截止时间": "2026-08-24",
                    "核查人": "来源分配人",
                    "姓名": "测试人员",
                    "身份证号": "320000000000000001",
                    "联系方式": "13800000000",
                    "地址": "测试地址",
                    "下发社区": "长板社区",
                    "核查结果": "",
                    "备注": "",
                }
            ]
        }

        engine = SyncEngine(None)
        with patch(
            "services.sync_engine.get_database_column_map",
            new=AsyncMock(return_value=column_map),
        ), patch.object(
            engine,
            "_save_snapshot",
            new=AsyncMock(return_value="2026-08-24"),
        ):
            count, report_date = await engine._sync_qmf_source(conn, result)

        self.assertEqual(count, 1)
        self.assertEqual(report_date, "2026-08-24")
        sql, params = conn.cursor_instance.calls[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("`截止时间`=VALUES(`截止时间`)", sql)
        self.assertIn("`下发社区`=VALUES(`下发社区`)", sql)
        self.assertIn("`核查人`=IF(TRIM(COALESCE(`核查人`,''))=''", sql)
        self.assertNotIn("`核查结果`=VALUES(`核查结果`)", sql)
        self.assertNotIn("`备注`=VALUES(`备注`)", sql)
        self.assertEqual(params[3], "测试人员")
        self.assertEqual(conn.events, ["begin", "commit"])

    async def test_empty_or_failed_source_does_not_touch_local_table(self):
        conn = RecordingConnection()
        engine = SyncEngine(None)
        with patch(
            "services.sync_engine.get_database_column_map",
            new=AsyncMock(return_value={}),
        ), patch.object(engine, "_save_snapshot", new=AsyncMock()) as snapshot:
            count, report_date = await engine._sync_qmf_source(
                conn,
                {"rows": []},
            )
        self.assertEqual((count, report_date), (0, None))
        self.assertEqual(conn.cursor_instance.calls, [])
        self.assertEqual(conn.events, [])
        snapshot.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
