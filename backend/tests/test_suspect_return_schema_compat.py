import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.query import query_data
from services.parsers.suspect_return import SuspectReturnParser
from services.schema_compat import get_database_column_map
from services.sync_engine import SyncEngine


def make_connection(*, fetchall_values=None, fetchone_value=None):
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(side_effect=fetchall_values)
    cursor.fetchone = AsyncMock(return_value=fetchone_value)

    cursor_context = MagicMock()
    cursor_context.__aenter__ = AsyncMock(return_value=cursor)
    cursor_context.__aexit__ = AsyncMock(return_value=None)

    connection = MagicMock()
    connection.cursor.return_value = cursor_context
    return connection, cursor


def legacy_archive_columns(parser):
    aliases = {
        "身份证号码": "身份证号",
        "二次核查结果": "二次反馈",
    }
    return [aliases.get(column, column) for column in parser.COLUMNS]


class SuspectReturnSchemaCompatTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_archive_columns_are_resolved(self):
        parser = SuspectReturnParser()
        available = legacy_archive_columns(parser)
        connection, cursor = make_connection(
            fetchall_values=[[(column,) for column in available]]
        )

        column_map = await get_database_column_map(
            connection,
            "OnlineDataArchive.t_suspect_return_archive",
            parser,
        )

        self.assertEqual(column_map["身份证号码"], "身份证号")
        self.assertEqual(column_map["二次核查结果"], "二次反馈")
        self.assertEqual(column_map["联系号码"], "联系号码")
        self.assertIn(
            "SHOW COLUMNS FROM OnlineDataArchive.t_suspect_return_archive",
            cursor.execute.await_args.args[0],
        )

    async def test_archive_insert_uses_legacy_names_and_canonical_values(self):
        parser = SuspectReturnParser()
        column_map = parser.resolve_database_columns(
            set(legacy_archive_columns(parser))
        )
        connection, cursor = make_connection()
        data = {column: f"value-{index}" for index, column in enumerate(parser.COLUMNS)}

        await SyncEngine(None)._archive(
            connection,
            parser.table_name,
            "row-key",
            data,
            parser,
            column_map,
        )

        insert_call, delete_call = cursor.execute.await_args_list
        insert_sql = insert_call.args[0]
        self.assertIn("`身份证号`", insert_sql)
        self.assertIn("`二次反馈`", insert_sql)
        self.assertNotIn("`身份证号码`", insert_sql)
        self.assertNotIn("`二次核查结果`", insert_sql)
        self.assertEqual(
            insert_call.args[1],
            ["row-key"] + [data[column] for column in parser.COLUMNS],
        )
        self.assertIn("DELETE FROM t_suspect_return", delete_call.args[0])

    async def test_archive_query_keeps_canonical_api_columns(self):
        parser = SuspectReturnParser()
        archive_columns = legacy_archive_columns(parser)
        row = tuple(f"value-{index}" for index in range(len(parser.COLUMNS)))
        connection, cursor = make_connection(
            fetchall_values=[
                [(column,) for column in archive_columns],
                [row],
            ],
            fetchone_value=(1,),
        )

        result = await query_data(
            "疑似返苏",
            source="archive",
            page=1,
            page_size=50,
            keyword="张",
            sort_by="身份证号码",
            sort_order="asc",
            filters=json.dumps({"二次核查结果": ["完成"]}),
            conn=connection,
        )

        count_sql = cursor.execute.await_args_list[1].args[0]
        select_sql = cursor.execute.await_args_list[2].args[0]
        self.assertIn("`身份证号` LIKE %s", count_sql)
        self.assertIn("`二次反馈` IN (%s)", count_sql)
        self.assertIn("ORDER BY `身份证号` ASC", select_sql)
        self.assertIn("`身份证号`", select_sql)
        self.assertIn("`二次反馈`", select_sql)
        self.assertEqual(result["columns"], parser.COLUMNS)
        self.assertEqual(result["data"][0]["身份证号码"], "value-5")
        self.assertEqual(result["data"][0]["二次核查结果"], "value-11")

    def test_missing_column_has_clear_error(self):
        parser = SuspectReturnParser()
        available = set(parser.COLUMNS) - {"身份证号码"}

        with self.assertRaisesRegex(RuntimeError, "身份证号码"):
            parser.resolve_database_columns(available)


if __name__ == "__main__":
    unittest.main()
