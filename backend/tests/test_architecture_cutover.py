import unittest
from types import SimpleNamespace

from services.architecture_cutover import get_cutover_state


class ArchitectureCutoverTests(unittest.TestCase):
    def test_default_upgrade_is_disabled_and_mysql_remains_authoritative(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=False,
                ARCHITECTURE_UPGRADE_DOMAIN="task_metadata",
                ARCHITECTURE_UPGRADE_READ_SOURCE="mysql",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=True,
            )
        )
        self.assertEqual(state.status, "disabled")
        self.assertFalse(state.ready)
        self.assertFalse(state.active)
        self.assertFalse(state.adapter_ready)
        self.assertEqual(state.authoritative_path, "mysql")
        self.assertEqual(state.reason, "upgrade_disabled")
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
        self.assertEqual(state.status, "mysql")
        self.assertFalse(state.ready)
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
        self.assertEqual(state.status, "not_ready")
        self.assertFalse(state.ready)
        self.assertEqual(state.reason, "flink_read_adapter_not_installed")
        self.assertTrue(state.fallback_configured)
        self.assertFalse(state.active)
        self.assertEqual(state.authoritative_path, "mysql")

    def test_disabled_flink_request_does_not_claim_readiness(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=False,
                ARCHITECTURE_UPGRADE_DOMAIN="task_metadata",
                ARCHITECTURE_UPGRADE_READ_SOURCE="flink",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=True,
            )
        )
        self.assertEqual(state.status, "disabled")
        self.assertFalse(state.ready)
        self.assertEqual(state.reason, "upgrade_disabled")

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
