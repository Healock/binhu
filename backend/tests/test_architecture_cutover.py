import unittest
from types import SimpleNamespace

from services.architecture_cutover import get_cutover_state


class ArchitectureCutoverTests(unittest.TestCase):
    def test_default_is_authoritative_mysql(self):
        state = get_cutover_state(
            SimpleNamespace(
                ARCHITECTURE_UPGRADE_ENABLED=False,
                ARCHITECTURE_UPGRADE_DOMAIN="task_metadata",
                ARCHITECTURE_UPGRADE_READ_SOURCE="mysql",
                ARCHITECTURE_UPGRADE_FALLBACK_ENABLED=True,
            )
        )
        self.assertEqual(state.status, "mysql")
        self.assertTrue(state.ready)
        self.assertEqual(state.reason, "authoritative_mysql_path")

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
        self.assertTrue(state.fallback_enabled)

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
