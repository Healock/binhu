from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class MigrationInfrastructureContractTests(unittest.TestCase):
    def test_wireguard_is_host_only_and_has_keepalive(self) -> None:
        script = (ROOT / "deploy/install-wireguard-link.sh").read_text(encoding="utf-8")
        self.assertIn("Address = 10.77.0.1/30", script)
        self.assertIn("Address = 10.77.0.2/30", script)
        self.assertIn("AllowedIPs = 10.77.0.2/32", script)
        self.assertIn("AllowedIPs = 10.77.0.1/32", script)
        self.assertIn("PersistentKeepalive = 25", script)
        self.assertNotIn("0.0.0.0/0", script)
        self.assertNotRegex(script, re.compile(r"sysctl.*ip_forward"))

    def test_offsite_receiver_has_fixed_command_and_source(self) -> None:
        installer = (ROOT / "deploy/install-offsite-backup.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('restrict,from=\\"10.77.0.2\\",command=', installer)
        self.assertIn("receive --inbox", installer)
        self.assertIn("binhu-backup-shell", installer)
        self.assertIn("usermod --password", installer)
        self.assertIn('chmod 0711 "$state_dir"', installer)
        self.assertNotIn("passwd -l", installer)
        self.assertNotIn("NOPASSWD", installer)
        self.assertIn("--retention-days 7", (
            ROOT / "deploy/systemd/binhu-offsite-ingest.service"
        ).read_text(encoding="utf-8"))

    def test_offsite_sender_requires_wireguard_mount_and_strict_host_key(self) -> None:
        service = (ROOT / "deploy/systemd/binhu-offsite-push.service").read_text(
            encoding="utf-8"
        )
        program = (ROOT / "deploy/binhu_offsite_backup.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("wg-quick@wg0.service", service)
        self.assertIn("ConditionPathIsMountPoint=/backup", service)
        self.assertIn("10.77.0.1", service)
        self.assertIn("StrictHostKeyChecking=yes", program)
        self.assertIn("PasswordAuthentication=no", program)
        self.assertNotIn("StrictHostKeyChecking=no", program)

    def test_nginx_profiles_keep_single_backend_and_safe_rollback(self) -> None:
        new_profile = (ROOT / "nginx/migration/new-production.conf").read_text(
            encoding="utf-8"
        )
        old_profile = (ROOT / "nginx/migration/old-proxy.conf.template").read_text(
            encoding="utf-8"
        )
        switcher = (ROOT / "deploy/binhu-nginx-profile").read_text(encoding="utf-8")
        self.assertIn("listen 10.77.0.2:18080", new_profile)
        self.assertIn("proxy_pass http://10.77.0.2:18080", old_profile)
        self.assertNotIn("frp-cat.com", old_profile)
        self.assertIn("nginx -t", switcher)
        self.assertIn("restore_previous", switcher)
        self.assertIn("systemctl reload nginx", switcher)

    def test_templates_do_not_contain_real_public_hosts_or_secrets(self) -> None:
        paths = [
            ROOT / "deploy/install-offsite-backup.sh",
            ROOT / "deploy/install-wireguard-link.sh",
            ROOT / "deploy/install-nginx-migration-profiles.sh",
            ROOT / "nginx/migration/old-maintenance.conf.template",
            ROOT / "nginx/migration/old-proxy.conf.template",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        self.assertNotRegex(combined, re.compile(r"(?i)(password|token|secret)\s*="))
        self.assertNotRegex(combined, re.compile(r"\b(?:47|221)\.\d+\.\d+\.\d+\b"))


if __name__ == "__main__":
    unittest.main()
