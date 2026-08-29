import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class ServerAssetTests(unittest.TestCase):
    linux_assets = (
        "binhu-renew-ip-certificate",
        "binhu-obtain-ip-certificate",
        "binhu-record-ip-certificate-failure",
        "install-server.sh",
        "systemd/binhu-ip-cert-renew.service",
        "systemd/binhu-ip-cert-renew-failed.service",
        "systemd/binhu-ip-cert-renew.timer",
        "nginx/binhu-updates-acme.inc",
    )

    def test_linux_assets_use_lf_only(self):
        for relative_path in self.linux_assets:
            with self.subTest(path=relative_path):
                content = (SERVER_ROOT / relative_path).read_bytes()
                self.assertNotIn(b"\r", content)

    def test_extensionless_certificate_scripts_are_forced_to_lf(self):
        attributes = (REPOSITORY_ROOT / ".gitattributes").read_text(encoding="utf-8")
        for relative_path in (
            "desktop/server/binhu-renew-ip-certificate",
            "desktop/server/binhu-obtain-ip-certificate",
            "desktop/server/binhu-record-ip-certificate-failure",
        ):
            with self.subTest(path=relative_path):
                self.assertIn(f"{relative_path} text eol=lf", attributes)

    def test_installer_rejects_crlf_before_installing_assets(self):
        installer = (SERVER_ROOT / "install-server.sh").read_text(encoding="utf-8")
        gate = installer.index("reject_crlf \\")
        gate_block = installer[gate : installer.index("[ -r /etc/os-release ]")]
        first_install = installer.index(
            "install -o root -g root -m 0755 binhu-renew-ip-certificate"
        )
        self.assertLess(gate, first_install)
        self.assertIn("grep -q \"$carriage_return\"", installer)
        for relative_path in self.linux_assets[0:3] + self.linux_assets[4:]:
            with self.subTest(path=relative_path):
                self.assertIn(relative_path, gate_block)

    def test_renewal_unit_records_exec_failures(self):
        renewal_unit = (
            SERVER_ROOT / "systemd/binhu-ip-cert-renew.service"
        ).read_text(encoding="utf-8")
        failure_unit = (
            SERVER_ROOT / "systemd/binhu-ip-cert-renew-failed.service"
        ).read_text(encoding="utf-8")
        self.assertIn("OnFailure=binhu-ip-cert-renew-failed.service", renewal_unit)
        self.assertIn(
            "ExecStart=/bin/sh /usr/local/sbin/binhu-renew-ip-certificate",
            renewal_unit,
        )
        self.assertIn(
            "ExecStart=/bin/sh /usr/local/sbin/binhu-record-ip-certificate-failure",
            failure_unit,
        )

    def test_certbot_does_not_add_a_second_random_delay(self):
        renewal_script = (SERVER_ROOT / "binhu-renew-ip-certificate").read_text(
            encoding="utf-8"
        )
        timer = (SERVER_ROOT / "systemd/binhu-ip-cert-renew.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("--no-random-sleep-on-renew", renewal_script)
        self.assertIn("RandomizedDelaySec=30m", timer)


if __name__ == "__main__":
    unittest.main()
