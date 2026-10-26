import unittest
from types import SimpleNamespace

from services.architecture_cutover import get_cutover_state


class ArchitectureCutoverTests(unittest.TestCase):
    def test_default_upgrade_is_available_but_mysql_remains_authoritative(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=False,
                ARCHITECTURE_UPGRADE_DOMAIN="task_metadata",
                ARCHITECTURE_UPGRADE_READ_SOURCE="mysql",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=True,
            )
        )
        self.assertEqual(state.status, "available")
        self.assertTrue(state.ready)
        self.assertTrue(state.available)
        self.assertFalse(state.active)
        self.assertTrue(state.adapter_ready)
        self.assertEqual(state.authoritative_path, "mysql")
        self.assertEqual(state.reason, "validated_scoped_domain_not_active")
        self.assertTrue(state.fallback_configured)

    def test_enabled_mysql_is_not_a_new_architecture_ready_state(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=True,
                ARCHITECTURE_UPGRADE_DOMAIN="task_metadata",
                ARCHITECTURE_UPGRADE_READ_SOURCE="mysql",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=False,
            )
        )
        self.assertEqual(state.status, "available")
        self.assertTrue(state.ready)
        self.assertTrue(state.available)
        self.assertFalse(state.active)
        self.assertFalse(state.fallback_configured)
        self.assertEqual(state.authoritative_path, "mysql")

    def test_flink_source_is_fail_closed_until_adapter_exists(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=True,
                ARCHITECTURE_UPGRADE_DOMAIN="task_metadata",
                ARCHITECTURE_UPGRADE_READ_SOURCE="flink",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=True,
            )
        )
        self.assertEqual(state.status, "available")
        self.assertTrue(state.ready)
        self.assertTrue(state.available)
        self.assertTrue(state.fallback_configured)
        self.assertFalse(state.active)
        self.assertEqual(state.authoritative_path, "mysql")

    def test_disabled_flink_request_remains_available_but_inactive(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=False,
                ARCHITECTURE_UPGRADE_DOMAIN="task_metadata",
                ARCHITECTURE_UPGRADE_READ_SOURCE="flink",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=True,
            )
        )
        self.assertEqual(state.status, "available")
        self.assertTrue(state.ready)
        self.assertTrue(state.available)
        self.assertFalse(state.active)
        self.assertEqual(state.reason, "validated_scoped_domain_not_active")

    def test_unknown_read_source_is_rejected(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=True,
                ARCHITECTURE_UPGRADE_DOMAIN="task_metadata",
                ARCHITECTURE_UPGRADE_READ_SOURCE="redis",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=True,
            )
        )
        self.assertEqual(state.status, "invalid")
        self.assertFalse(state.ready)
        self.assertEqual(state.reason, "unsupported_read_source")

    def test_online_source_domain_is_supported_for_flink_freshness_gate(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=True,
                ARCHITECTURE_UPGRADE_DOMAIN="online_source",
                ARCHITECTURE_UPGRADE_READ_SOURCE="flink",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=True,
            )
        )
        self.assertTrue(state.available)
        self.assertTrue(state.ready)
        self.assertEqual(state.domain, "online_source")

    def test_online_source_reports_active_only_with_runtime_contract(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=True,
                ARCHITECTURE_UPGRADE_DOMAIN="online_source",
                ARCHITECTURE_UPGRADE_READ_SOURCE="flink",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=True,
                ARCHITECTURE_UPGRADE_RUN_ID="STG-20261026-01",
                ARCHITECTURE_UPGRADE_DERIVED_DATABASE="Staging_EventPipeline",
            )
        )
        self.assertTrue(state.active)
        self.assertEqual(state.status, "active")

    def test_unknown_domain_is_rejected_without_activation(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=True,
                ARCHITECTURE_UPGRADE_DOMAIN="daily_report",
                ARCHITECTURE_UPGRADE_READ_SOURCE="flink",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=False,
            )
        )
        self.assertEqual(state.status, "invalid")
        self.assertFalse(state.ready)
        self.assertEqual(state.reason, "unsupported_domain")


if __name__ == "__main__":
    unittest.main()
