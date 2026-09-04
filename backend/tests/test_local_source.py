import os
import asyncio
import inspect
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.local_source import (
    LOCAL_SPREADSHEET_ID,
    cleanup_duplicate_local_sources,
    ensure_local_source_schema,
    local_data_source_enabled,
    local_row_hash,
    local_sheet_id,
    mirror_business_tables_to_local_sources,
    stable_json,
)
from services.online_source import active_source_sql_filter
from services.online_local_writeback import apply_local_system_changes
from services.parsers import get_parser
from services import admin_task_queue
from services import photo_sheet_sync, sync_tasks
from routers import query
from config import settings


class LocalSourceHelpersTest(unittest.TestCase):
    def test_duplicate_cleanup_is_conservative_and_explicitly_applied(self):
        source = inspect.getsource(cleanup_duplicate_local_sources)
        self.assertIn("spreadsheet_id=0", source)
        self.assertIn("archived_at IS NULL", source)
        self.assertIn("len(hashes) != 1", source)
        self.assertIn("source_kind='superseded'", source)
        self.assertIn("if not apply", source)
    def test_local_locator_is_stable_and_not_a_tencent_position(self):
        self.assertEqual(LOCAL_SPREADSHEET_ID, 0)
        self.assertEqual(local_sheet_id("全链条"), "local:全链条")

    def test_stable_json_and_hash_are_order_independent(self):
        left = {"身份证号": "320000000000000000", "社区": "长板"}
        right = {"社区": "长板", "身份证号": "320000000000000000"}
        self.assertEqual(stable_json(left), stable_json(right))
        self.assertEqual(local_row_hash(left), local_row_hash(right))

    def test_local_source_cannot_be_disabled_by_legacy_setting(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            self.assertTrue(local_data_source_enabled())
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", False):
            self.assertTrue(local_data_source_enabled())

    def test_task_source_filter_excludes_legacy_rows_during_local_cutover(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            clause = active_source_sql_filter("全链条", "source_row")
        self.assertIn("source_row.spreadsheet_id=0", clause)
        self.assertIn(
            "source_row.source_kind IN ('local_table','local_dispatch')",
            clause,
        )
        self.assertNotIn("legacy-model-three", clause)

        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            model_three_clause = active_source_sql_filter(
                "疑似未注销模型三", "source_row"
            )
        self.assertIn("source_row.spreadsheet_id=0", model_three_clause)
        self.assertIn(
            "source_row.source_kind IN ('local_table','local_dispatch')",
            model_three_clause,
        )
        self.assertNotIn("legacy-model-three", model_three_clause)

        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", False):
            clause = active_source_sql_filter("全链条")
        self.assertIn("spreadsheet_id=0", clause)

    def test_mirror_reuses_archived_local_physical_position(self):
        source = inspect.getsource(mirror_business_tables_to_local_sources)
        self.assertIn("sheet_id=%s AND physical_row=%s", source)
        self.assertIn("ORDER BY archived_at IS NULL DESC,id", source)

    def test_schema_only_rewrites_the_retired_source_default_when_needed(self):
        source = inspect.getsource(ensure_local_source_schema)
        self.assertIn('column_info[4]', source)
        self.assertIn('!= "local_table"', source)
        self.assertIn("MODIFY COLUMN `source_kind`", source)

    def test_local_system_change_supersedes_dispatch_record(self):
        class Cursor:
            def __init__(self):
                self.calls = []
                self.one = None
                self.rowcount = 0
                self.lastrowid = 41

            async def execute(self, sql, params=None):
                compact = " ".join(sql.split())
                self.calls.append((compact, params))
                self.one = None
                self.rowcount = 1
                if compact.startswith("SELECT source_kind,source_ref"):
                    self.one = ("local_dispatch", "police_dispatch_task:7")
                elif compact.startswith("SELECT id FROM `t_fullchain`"):
                    self.one = (12,)
                elif compact.startswith("UPDATE _local_source_records SET parser_type"):
                    self.rowcount = 0

            async def fetchone(self):
                return self.one

            async def executemany(self, sql, params):
                self.calls.append((" ".join(sql.split()), params))
                self.rowcount = len(params)

        parser = get_parser("全链条")
        values = {column: "" for column in parser.COLUMNS}
        values.update({
            "姓名": "测试人员",
            "身份证号": "32052519911016025X",
            "电话号码": "13800138000",
            "下发时间": "2026-08-29",
        })
        cursor = Cursor()
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            asyncio.run(apply_local_system_changes(
                cursor,
                source={
                    "id": 9,
                    "row_key": parser.make_row_key(values),
                    "revision": 1,
                    "physical_row": 12,
                    "values": values,
                    "spreadsheet": {"parser_type": "全链条"},
                },
                changes={"核查人": "测试网格员"},
                rebuild=False,
            ))

        supersede = [
            call for call in cursor.calls
            if "SET status='superseded'" in call[0]
        ]
        self.assertEqual(
            supersede[0][1],
            ("local_dispatch", "police_dispatch_task:7"),
        )
        self.assertTrue(any(
            "INSERT INTO _online_projection_jobs" in call[0]
            for call in cursor.calls
        ))

    def test_local_query_editing_does_not_depend_on_tencent_writeback_switch(self):
        projection_source = inspect.getsource(query._projection_query)
        source_rows_source = inspect.getsource(query.list_source_rows)
        expected = "local_data_source_enabled() or await _writeback_enabled(cur)"
        self.assertIn(expected, projection_source)
        self.assertIn(expected, source_rows_source)

    def test_local_mode_hides_legacy_external_queues(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            self.assertFalse(hasattr(admin_task_queue, "_sync_jobs"))
            self.assertFalse(hasattr(admin_task_queue, "_writeback_queues"))
            details = asyncio.run(
                admin_task_queue.get_admin_task_queue_details(
                    "online_writeback_queue"
                )
            )
        self.assertEqual(details["total"], 0)
        self.assertIn("本地数据源已启用", details["message"])

    def test_local_mode_rejects_new_tencent_sync_tasks(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            result = asyncio.run(sync_tasks.create_sync_task("manual", requested_by=7))
        self.assertEqual(result, (0, "disabled", "腾讯数据源已下线，无需创建同步任务"))

    def test_local_mode_does_not_claim_scheduled_tencent_sync_tasks(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            result = asyncio.run(sync_tasks.claim_due_scheduled_task())
        self.assertIsNone(result)

    def test_local_mode_disables_photo_sheet_operations_without_external_access(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            outbox = asyncio.run(photo_sheet_sync.process_outbox_once())
            sync = asyncio.run(photo_sheet_sync.sync_online_once())
            self.assertFalse(photo_sheet_sync.launch_daily_full_sync())
        self.assertTrue(outbox["disabled"])
        self.assertTrue(sync["disabled"])


if __name__ == "__main__":
    unittest.main()
