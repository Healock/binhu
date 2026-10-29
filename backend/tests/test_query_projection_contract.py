import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from routers import query


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.fetchone_values = [(len(rows),), ("{}",), (0,)]
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, sql, params=()):
        self.executed.append((sql, params))

    async def fetchone(self):
        return self.fetchone_values.pop(0)

    async def fetchall(self):
        if len(self.executed) == 2:
            return self.rows
        return []


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class QueryProjectionContractTests(unittest.TestCase):
    def test_flink_projection_metadata_is_scoped_to_projection_query(self):
        row = (
            "t_fullchain:42",
            json.dumps({}),
            1,
            0,
            None,
            42,
            7,
            "row-hash",
            "{}",
            12,
        )
        cursor = _Cursor([row])
        derived = AsyncMock(return_value={"revision": 9})
        metadata = {
            column: {"type": "text"}
            for column in query.get_parser("全链条").COLUMNS
        }
        with patch.object(query, "local_data_source_enabled", return_value=False), \
                patch.object(query, "effective_view_communities", return_value=None), \
                patch.object(query, "inspector_option_context", AsyncMock(return_value={})), \
                patch.object(query, "_managed_column_metadata", AsyncMock(return_value=metadata)), \
                patch.object(query, "_query_row_edit_capabilities", AsyncMock(return_value={"editable_fields": []})), \
                patch.object(query, "_writeback_enabled", AsyncMock(return_value=True)), \
                patch.object(query, "_source_data_version", AsyncMock(return_value="1:7:")), \
                patch.object(query, "selected_engine", return_value="flink"), \
                patch.object(query, "read_derived_projection", derived), \
                patch.object(settings, "APP_ENVIRONMENT", "staging"), \
                patch.object(settings, "ARCHITECTURE_UPGRADE_DOMAIN", "online_source"), \
                patch.object(settings, "ARCHITECTURE_UPGRADE_READ_SOURCE", "flink"), \
                patch.object(settings, "ARCHITECTURE_UPGRADE_RUN_ID", "STG-20261029-15"), \
                patch.object(settings, "ARCHITECTURE_UPGRADE_DERIVED_DATABASE", "Staging_EventPipeline"), \
                patch.object(settings, "ARCHITECTURE_UPGRADE_FALLBACK_ENABLED", True):
            result = asyncio.run(query._projection_query(
                parser_type="全链条",
                page=1,
                page_size=20,
                keyword=None,
                sort_by=None,
                sort_order="desc",
                filters=None,
                grid_filters=None,
                user={},
                conn=_Connection(cursor),
                spreadsheets=[],
            ))

        record = result["data"][0]
        self.assertEqual(record["__projection_engine"], "flink")
        self.assertEqual(record["__projection_status"], "flink_freshness_checked")
        self.assertEqual(record["__projection_revision"], 9)
        derived.assert_awaited_once_with(
            cursor,
            environment="staging",
            run_id="STG-20261029-15",
            task_id="t_fullchain:42",
            source_id=42,
            engine="flink",
            database="Staging_EventPipeline",
        )


if __name__ == "__main__":
    unittest.main()
