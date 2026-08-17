import os
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.qmf_config import (  # noqa: E402
    QmfRuntimeConfig,
    decrypt_secret,
    encrypt_secret,
    load_qmf_config,
    public_config,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, *_args):
        return None

    async def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


class QmfConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_secret_is_encrypted_at_rest(self):
        encrypted = encrypt_secret("password-value")
        self.assertTrue(encrypted.startswith("v1:"))
        self.assertNotIn("password-value", encrypted)
        self.assertEqual(decrypt_secret(encrypted), "password-value")
        self.assertEqual(decrypt_secret("not-an-encrypted-value"), "")

    async def test_database_values_override_environment_and_public_view_is_write_only(self):
        rows = [
            ("qmf_preview_enabled", "1"),
            ("qmf_registration_enabled", "1"),
            ("qmf_login_protocol_verified", "1"),
            ("qmf_write_protocol_verified", "1"),
            ("qmf_api_base_url", "http://qmf.example/grid_terminal_interface/"),
            ("qmf_login_host", "180.97.151.38"),
            ("qmf_login_port", "25001"),
            ("qmf_source_username", encrypt_secret("source-user")),
            ("qmf_source_password", encrypt_secret("source-password")),
            ("qmf_source_imei", encrypt_secret("imei-value")),
            ("qmf_source_machine_uid", encrypt_secret("machine-value")),
            ("qmf_expected_station_code", "320584710000"),
            ("qmf_expected_station_name", "滨湖新城派出所"),
            ("qmf_status_scan_enabled", "1"),
            ("qmf_status_scan_time", "07:00"),
        ]
        config = await load_qmf_config(_Conn(rows))
        self.assertTrue(config.configured)
        self.assertTrue(config.registration_configured)
        self.assertEqual(config.source_username, "source-user")
        self.assertEqual(config.source_password, "source-password")
        self.assertEqual(config.source_imei, "imei-value")
        self.assertEqual(config.source_machine_uid, "machine-value")
        self.assertTrue(config.status_scan_enabled)
        self.assertEqual(config.status_scan_time, "07:00")

        public = public_config(config, {row[0] for row in rows})
        self.assertTrue(public["source_password_configured"])
        self.assertNotIn("source_password", public)
        self.assertEqual(public["source_imei"], "imei-value")
        self.assertEqual(public["source_machine_uid"], "machine-value")

    def test_unconfigured_runtime_is_closed(self):
        config = QmfRuntimeConfig(
            preview_enabled=True,
            registration_enabled=False,
            login_protocol_verified=True,
            write_protocol_verified=False,
            api_base_url="",
            login_host="",
            login_port=0,
            source_username="",
            source_password="",
            source_imei="",
            source_machine_uid="",
            expected_station_code="320584710000",
            expected_station_name="滨湖新城派出所",
            timeout_seconds=15,
            session_max_seconds=45,
        )
        self.assertFalse(config.configured)
        self.assertFalse(config.registration_configured)

    def test_registration_requires_both_switches_and_protocol_confirmations(self):
        base = dict(
            preview_enabled=True,
            registration_enabled=True,
            login_protocol_verified=True,
            write_protocol_verified=True,
            api_base_url="http://qmf.invalid/grid_terminal_interface/",
            login_host="qmf.invalid",
            login_port=25001,
            source_username="fictional-user",
            source_password="fictional-password",
            source_imei="fictional-imei",
            source_machine_uid="Fictional Device",
            expected_station_code="320584710000",
            expected_station_name="滨湖新城派出所",
            timeout_seconds=15,
            session_max_seconds=45,
        )
        self.assertTrue(QmfRuntimeConfig(**base).registration_configured)
        for disabled_field in (
            "preview_enabled",
            "registration_enabled",
            "login_protocol_verified",
            "write_protocol_verified",
        ):
            with self.subTest(disabled_field=disabled_field):
                self.assertFalse(QmfRuntimeConfig(
                    **{**base, disabled_field: False}
                ).registration_configured)
