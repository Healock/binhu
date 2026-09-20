import inspect
import os
import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services import txdocs_statistics_monitor as monitor


def variant(key: str, content: str, community: str = "社区甲") -> monitor.MonitorVariant:
    return monitor.MonitorVariant(
        business_key_hash=key * 64,
        content_hash=content * 64,
        community_hash=("c" if community == "社区甲" else "d") * 64,
        community=community,
    )


class TxDocsStatisticsMonitorTests(unittest.IsolatedAsyncioTestCase):
    def test_monitoring_environment_is_production_only(self):
        with patch.object(monitor.settings, "APP_ENVIRONMENT", "production"):
            self.assertTrue(monitor.monitoring_environment_allowed())
        for environment in ("development", "staging", "shadow"):
            with patch.object(monitor.settings, "APP_ENVIRONMENT", environment):
                self.assertFalse(monitor.monitoring_environment_allowed())

    def test_allowlist_is_fixed_positive_ids(self):
        self.assertEqual(monitor.monitoring_spreadsheet_ids("3, 2, 3"), (3, 2))
        for invalid in ("0", "-1", "1,abc"):
            with self.assertRaises(ValueError):
                monitor.monitoring_spreadsheet_ids(invalid)

    def test_first_snapshot_is_a_baseline_not_mass_addition(self):
        current = Counter({variant("a", "1"): 2})

        delta = monitor.compare_monitor_snapshots(
            Counter(), current, has_baseline=False
        )

        self.assertEqual(delta.current_total, 2)
        self.assertEqual((delta.added, delta.changed, delta.removed), (0, 0, 0))

    def test_reordering_and_identical_duplicates_do_not_create_changes(self):
        snapshot = Counter({variant("a", "1"): 2, variant("b", "2"): 1})

        delta = monitor.compare_monitor_snapshots(
            snapshot.copy(), snapshot.copy(), has_baseline=True
        )

        self.assertEqual(delta.current_total, 3)
        self.assertEqual((delta.added, delta.changed, delta.removed), (0, 0, 0))

    def test_same_business_key_content_edit_is_one_change(self):
        previous = Counter({variant("a", "1"): 1})
        current = Counter({variant("a", "2"): 1})

        delta = monitor.compare_monitor_snapshots(
            previous, current, has_baseline=True
        )

        self.assertEqual((delta.added, delta.changed, delta.removed), (0, 1, 0))
        self.assertEqual(delta.communities["社区甲"]["changed"], 1)

    def test_count_growth_and_removal_are_not_misclassified_as_edits(self):
        previous = Counter({variant("a", "1"): 1, variant("b", "2"): 2})
        current = Counter({variant("a", "1"): 3, variant("b", "2"): 1})

        delta = monitor.compare_monitor_snapshots(
            previous, current, has_baseline=True
        )

        self.assertEqual((delta.added, delta.changed, delta.removed), (2, 0, 1))

    def test_snapshot_discards_personal_text_and_physical_rows(self):
        source = [{
            "physical_row": 87,
            "values": {
                "下发日期": "2026-09-14",
                "截止日期": "2026-09-15",
                "核查人": "测试人员",
                "社区": "虚构社区",
                "来源": "测试",
                "姓名": "虚构甲",
                "身份证号": "320000190001010000",
                "电话号码": "13000000000",
                "地址": "测试路1号",
                "创建时间": "",
                "现住址": "测试路2号",
                "核查结果": "",
                "研判": "",
                "二次反馈": "",
                "登记情况": "",
            },
        }]

        snapshot, unkeyed = monitor.build_monitor_snapshot(7, "全链条", source)

        self.assertEqual(sum(snapshot.values()), 1)
        self.assertEqual(unkeyed, 0)
        serialized = repr(snapshot)
        for forbidden in ("虚构甲", "320000190001010000", "13000000000", "测试路1号"):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("虚构社区", serialized)
        self.assertFalse(hasattr(next(iter(snapshot)), "physical_row"))

    def test_business_buckets_use_dispatch_date_and_workflow_state(self):
        rows = [
            {"values": {
                "下发日期": "2026-09-19", "截止日期": "2026-09-20",
                "核查人": " 测试人员甲 ",
                "社区": "虚构社区", "身份证号": "320000190001010000",
                "电话号码": "13000000000", "核查结果": "",
            }},
            {"values": {
                "下发日期": "2026-09-10", "截止日期": "2026-09-11",
                "核查人": "测试人员乙",
                "社区": "虚构社区", "身份证号": "320000190001010001",
                "电话号码": "13000000001", "核查结果": "已登记",
            }},
        ]
        buckets = monitor.build_monitor_business_buckets(
            "全链条", rows, datetime(2026, 9, 19).date()
        )
        counts = {
            (bucket.checker_name, bucket.dispatch_date.isoformat(), bucket.task_state): count
            for bucket, count in buckets.items()
        }
        self.assertEqual(counts[("测试人员甲", "2026-09-19", "unchecked")], 1)
        self.assertEqual(counts[("测试人员乙", "2026-09-10", "completed")], 1)

    def test_external_overlay_uses_formal_community_for_alias_buckets(self):
        aliases = {
            "长板社区": "长板社区",
            "长板村": "长板社区",
        }

        self.assertEqual(
            monitor.canonical_monitor_community(" 长板村 ", aliases),
            "长板社区",
        )
        self.assertEqual(
            monitor.canonical_monitor_community("长板社区", aliases),
            "长板社区",
        )
        self.assertEqual(
            monitor.canonical_monitor_community("未配置来源", aliases),
            "未配置来源",
        )

    def test_model_three_is_supported_as_a_read_only_monitor_target(self):
        rows = [{"values": {
            "截止时间": "2026-09-20",
            "核查人": "虚构核查人",
            "姓名": "虚构人员",
            "身份证号": "SYNTHETIC-ID-002",
            "联系方式": "SYNTHETIC-CONTACT-002",
            "地址": "虚构地址",
            "下发社区": "虚构社区",
            "核查结果": "在吴",
        }}]
        buckets = monitor.build_monitor_business_buckets(
            "疑似未注销模型三", rows, datetime(2026, 9, 20).date()
        )
        bucket = next(iter(buckets))
        assert bucket.community == "虚构社区"
        assert bucket.checker_name == "虚构核查人"
        assert bucket.task_state == "completed"

    async def test_disabled_switch_makes_no_database_or_network_access(self):
        with patch.object(monitor.settings, "APP_ENVIRONMENT", "staging"):
            self.assertEqual(await monitor.run_txdocs_statistics_once(), 0)

    async def test_manual_failure_diagnostics_return_only_safe_codes(self):
        boundary = datetime(2026, 9, 19, 11, 46, 5)

        class Cursor:
            query = ""
            params = None

            async def execute(self, query, params=None):
                self.query = query
                self.params = params

            async def fetchall(self):
                return [
                    ("txdocs_400006",),
                    ("invalid_sheet_layout",),
                    ("",),
                    (None,),
                ]

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()

            def cursor(self):
                return self.cursor_instance

        class Pool:
            def __init__(self):
                self.connection = Connection()
                self.released = False

            async def acquire(self):
                return self.connection

            def release(self, conn):
                self.released = conn is self.connection

        pool = Pool()
        with patch("database.db_manager.get_pool", return_value=pool):
            result = await monitor.get_txdocs_monitor_failure_codes_since(boundary)

        self.assertEqual(result, ("txdocs_400006", "invalid_sheet_layout"))
        self.assertIn("_txdocs_monitor_runs", pool.connection.cursor_instance.query)
        self.assertEqual(pool.connection.cursor_instance.params, (boundary,))
        self.assertTrue(pool.released)

    async def test_legacy_allowlist_is_not_a_runtime_configuration(self):
        class Cursor:
            def __init__(self, credentials, configs):
                self.credentials = credentials
                self.configs = configs
                self.query = ""

            async def execute(self, query, params=None):
                self.query = query

            async def fetchone(self):
                return self.credentials if "oauth" in self.query else None

            async def fetchall(self):
                return self.configs

        with (
            patch.object(monitor.settings, "APP_ENVIRONMENT", "production"),
            patch.object(
                monitor.settings, "TXDOCS_MONITORING_SPREADSHEET_IDS", "7,8"
            ),
        ):
            self.assertFalse(
                await monitor.monitoring_configuration_ready(
                    Cursor(("client", "token", "open"), [(7, "全链条"), (8, "出租房屋核查")])
                )
            )
            self.assertFalse(await monitor.monitoring_configuration_ready(
                Cursor(None, [(7, "全链条"), (8, "出租房屋核查")])
            ))

    async def test_empty_target_configuration_does_not_read_legacy_snapshot(self):
        class Cursor:
            def __init__(self):
                self.results = iter([
                    (0,),
                    (0, 0, 0),
                    (2,),
                    (datetime.now(timezone.utc).replace(tzinfo=None), 2),
                    (0,),
                ])

            async def execute(self, query, params=None):
                return None

            async def fetchone(self):
                return next(self.results)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        class Connection:
            def __init__(self):
                self._cursor = Cursor()

            def cursor(self):
                return self._cursor

        class Pool:
            def __init__(self):
                self.connection = Connection()

            async def acquire(self):
                return self.connection

            def release(self, conn):
                return None

        pool = Pool()
        with (
            patch.object(monitor.settings, "APP_ENVIRONMENT", "production"),
            patch.object(
                monitor.settings, "TXDOCS_MONITORING_SPREADSHEET_IDS", "7"
            ),
            patch("database.db_manager.get_pool", return_value=pool),
        ):
            result = await monitor.get_txdocs_statistics_overview(
                "2026-09-14",
                "2026-09-14",
                ["全链条"],
                None,
                configuration_ready=True,
            )

        self.assertEqual(result["current_rows"], 0)
        self.assertEqual(result["successful_reads"], 0)
        self.assertEqual(result["status"], "healthy")

    def test_monitor_read_path_does_not_call_tencent_write_methods(self):
        source = inspect.getsource(monitor._read_config)
        for forbidden in (
            "batch_update", "clear_range", "clear_cell", "ensure_sheet",
            "build_delete_row_request",
        ):
            self.assertNotIn(forbidden, source)

    def test_monitor_schema_has_no_remote_body_or_physical_row_columns(self):
        source = inspect.getsource(monitor.ensure_txdocs_statistics_schema)
        self.assertIn("business_key_hash", source)
        self.assertIn("content_hash", source)
        self.assertNotIn("first_seen_at", source)
        self.assertIn("checker_name", source)
        for forbidden in (
            "identity_number", "phone", "address", "values_json",
            "physical_row", "access_token", "client_secret",
        ):
            self.assertNotIn(forbidden, source)

    def test_monitor_configuration_supports_shared_credentials_and_multiple_targets(self):
        config_source = inspect.getsource(monitor.ensure_txdocs_monitor_config_schema)
        loader_source = inspect.getsource(monitor._load_monitor_targets)
        self.assertIn("_txdocs_monitor_connection", config_source)
        self.assertIn("_txdocs_monitor_target", config_source)
        self.assertIn("UNIQUE KEY uq_txdocs_monitor_target", config_source)
        self.assertIn("include_legacy", loader_source)
        self.assertIn("enabled_only", loader_source)

    def test_monitor_runtime_is_separate_from_retired_business_switch(self):
        source = inspect.getsource(monitor.run_txdocs_statistics_once)
        self.assertIn("monitoring_environment_allowed", source)
        self.assertNotIn("TXDOCS_MONITORING_ENABLED", source)
        self.assertNotIn("TXDOCS_ENABLED", source)

    def test_schema_migration_does_not_use_deprecated_mysql_values_expression(self):
        source = inspect.getsource(monitor.ensure_txdocs_monitor_config_schema)
        self.assertNotIn("VALUES(client_id)", source)
        self.assertIn("NOT EXISTS", source)

    def test_scheduler_can_select_individual_target_intervals(self):
        source = inspect.getsource(monitor.run_txdocs_statistics_monitor)
        self.assertIn("target_ids", inspect.getsource(monitor.run_txdocs_statistics_once))
        self.assertIn("interval_seconds", source)


if __name__ == "__main__":
    unittest.main()
