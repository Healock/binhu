import unittest
from datetime import datetime, timezone

from fastapi import HTTPException

from services.maintenance import (
    enforce_maintenance,
    is_super_admin_user,
    maintenance_status,
    parse_utc_datetime,
    validate_maintenance_config,
)


class MaintenanceModeTests(unittest.TestCase):
    def test_disabled_and_scheduled_states(self):
        now = datetime(2026, 8, 8, 2, 0, tzinfo=timezone.utc)
        disabled = maintenance_status({"maintenance_enabled": "0"}, now=now)
        self.assertFalse(disabled["active"])

        scheduled = maintenance_status(
            {
                "maintenance_enabled": "1",
                "maintenance_start_at": "2026-08-08T03:00:00Z",
                "maintenance_end_at": "2026-08-08T04:00:00Z",
            },
            now=now,
        )
        self.assertTrue(scheduled["scheduled"])
        self.assertFalse(scheduled["active"])

    def test_active_and_expired_states(self):
        active = maintenance_status(
            {
                "maintenance_enabled": "1",
                "maintenance_start_at": "2026-08-08T02:00:00Z",
                "maintenance_end_at": "2026-08-08T04:00:00Z",
                "maintenance_message": "升级中",
            },
            now=datetime(2026, 8, 8, 2, 30, tzinfo=timezone.utc),
        )
        self.assertTrue(active["active"])
        self.assertEqual(active["message"], "升级中")

        expired = maintenance_status(
            {
                "maintenance_enabled": "1",
                "maintenance_end_at": "2026-08-08T02:00:00Z",
            },
            now=datetime(2026, 8, 8, 2, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(expired["active"])

    def test_validation_normalizes_to_utc_and_rejects_invalid_range(self):
        normalized = validate_maintenance_config(
            {
                "maintenance_enabled": True,
                "maintenance_start_at": "2026-08-08T10:00:00+08:00",
                "maintenance_end_at": "2026-08-08T11:00:00+08:00",
                "maintenance_message": "升级中",
            }
        )
        self.assertEqual(normalized["maintenance_enabled"], "1")
        self.assertEqual(normalized["maintenance_start_at"], "2026-08-08T02:00:00Z")
        self.assertEqual(normalized["maintenance_end_at"], "2026-08-08T03:00:00Z")

        with self.assertRaises(ValueError):
            validate_maintenance_config(
                {
                    "maintenance_start_at": "2026-08-08T04:00:00Z",
                    "maintenance_end_at": "2026-08-08T03:00:00Z",
                }
            )

    def test_only_super_admin_is_allowed_during_maintenance(self):
        config = {"maintenance_enabled": "1"}
        with self.assertRaises(HTTPException) as raised:
            enforce_maintenance(config, {"role": "member"})
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "maintenance_mode")

        self.assertTrue(is_super_admin_user({"role": "super_admin"}))
        self.assertTrue(is_super_admin_user({
            "role": "admin",
            "permission_groups": [{"code": "super_admin"}],
        }))
        enforce_maintenance(config, {"role": "super_admin"})

    def test_parse_naive_database_datetime_as_utc(self):
        parsed = parse_utc_datetime(datetime(2026, 8, 8, 2, 0))
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.hour, 2)


if __name__ == "__main__":
    unittest.main()
