import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]


class VenueCloudDeploymentContractTests(unittest.TestCase):
    def test_compose_isolated_and_not_public_mysql(self):
        compose = (ROOT / "deploy/venue-cloud/docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:48727:48727"', compose)
        self.assertIn("internal: true", compose)
        self.assertIn("  ingress:", compose)
        self.assertIn('command: ["python", "-m", "app.ingress_proxy"]', compose)
        receiver_block = compose.split("  receiver:", 1)[1].split("  ingress:", 1)[0]
        ingress_block = compose.split("  ingress:", 1)[1].split("networks:", 1)[0]
        self.assertNotIn("ports:", receiver_block)
        self.assertIn('"127.0.0.1:48727:48727"', ingress_block)
        self.assertNotIn("env_file:", ingress_block)
        self.assertNotIn("/run/secrets", ingress_block)
        self.assertNotIn('"3306:3306"', compose)
        self.assertIn("/srv/binhu-venue/mysql", compose)
        self.assertNotIn("/srv/binhu-updates", compose)
        self.assertIn("/etc/binhu-venue/mysql.env", compose)
        self.assertNotIn("MYSQL_ROOT_PASSWORD", (ROOT / "deploy/venue-cloud/receiver.env.example").read_text(encoding="utf-8"))

    def test_loopback_ingress_proxy_is_fixed_to_receiver(self):
        proxy = (ROOT / "cloud/venue-receiver/app/ingress_proxy.py").read_text(encoding="utf-8")
        self.assertIn('TARGET_HOST = os.getenv("INGRESS_TARGET_HOST", "receiver")', proxy)
        self.assertIn("asyncio.start_server", proxy)
        self.assertNotIn("subprocess", proxy)

    def test_nginx_keeps_updates_out_of_venue_include(self):
        nginx = (ROOT / "deploy/venue-cloud/nginx-server-locations.conf").read_text(encoding="utf-8")
        self.assertIn("/venue/", nginx)
        self.assertIn("/api/public/", nginx)
        self.assertIn("/api/internal/", nginx)
        self.assertIn("$ssl_client_verify", nginx)
        self.assertIn('Cache-Control "no-store"', nginx)
        self.assertIn("/www/wwwlogs/binhu-venue-access.log", nginx)
        self.assertIn("/www/wwwlogs/binhu-venue-internal.log", nginx)
        self.assertNotIn("/var/log/nginx", nginx)
        self.assertNotIn("location /updates", nginx)

    def test_workflow_is_manual_and_uses_restricted_gateway(self):
        workflow = (ROOT / ".github/workflows/venue-cloud-deploy.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("binhu-venue-publish@47.100.44.36", workflow)
        self.assertIn("-p 51234", workflow)
        self.assertIn("git merge-base --is-ancestor", workflow)
        self.assertNotIn("BINHU_UPDATE_SSH_KEY", workflow)

    def test_publish_gateway_parses_ssh_command_before_sudo(self):
        wrapper = (ROOT / "deploy/venue-cloud/binhu-venue-publish-gateway").read_text(encoding="utf-8")
        implementation = (ROOT / "deploy/venue-cloud/binhu-venue-publish-gateway.py").read_text(encoding="utf-8")
        self.assertIn("SSH_ORIGINAL_COMMAND", wrapper)
        self.assertIn("/usr/local/libexec/binhu-venue-publish-gateway.py", wrapper)
        self.assertIn("fcntl.flock", implementation)
        self.assertIn("STATE / \"publish.lock\"", implementation)

    def test_migration_tool_is_read_only_by_default(self):
        tool = (ROOT / "backend/tools/venue_cloud_migration.py").read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--apply", action="store_true"', tool)
        self.assertIn("--backup-reference", tool)
        self.assertIn("--expected-active-count", tool)
        self.assertIn("if not args.apply", tool)

    def test_server_preflight_and_migration_entrypoints_are_present(self):
        preflight = (ROOT / "deploy/venue-cloud/validate-server.sh").read_text(encoding="utf-8")
        migration = (ROOT / "deploy/venue-cloud/migrate.sh").read_text(encoding="utf-8")
        installer = (ROOT / "deploy/venue-cloud/install-server.sh").read_text(encoding="utf-8")
        self.assertIn("Read-only production preflight", preflight)
        self.assertIn("python -m app.migrate", migration)
        self.assertIn("/srv/binhu-venue/state/current.env", migration)
        self.assertIn("/usr/local/sbin/binhu-venue-validate", installer)
        self.assertIn("/usr/local/sbin/binhu-venue-migrate", installer)
        self.assertIn("/usr/local/sbin/binhu-venue-install-docker", installer)
        self.assertIn("/usr/local/sbin/binhu-venue-activate-nginx", installer)

    def test_nginx_activation_is_gated_and_reversible(self):
        activation = (ROOT / "deploy/venue-cloud/activate-nginx.sh").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:48727/health/ready", activation)
        self.assertIn("nginx -t", activation)
        self.assertIn("restore()", activation)
        self.assertIn("bt_proxy.conf.disabled-binhu-venue", activation)
        self.assertIn("/updates/win10-x64/releases.stable.json", activation)
        self.assertIn("internal API without mTLS", activation)
        self.assertIn("while [ \"$i\" -lt 10 ]", activation)

    def test_docker_installer_does_not_remove_conflicts_or_start_workload(self):
        installer = (ROOT / "deploy/venue-cloud/install-docker-engine.sh").read_text(encoding="utf-8")
        self.assertIn("docker-compose-plugin", installer)
        self.assertIn("at least 10 GiB", installer)
        self.assertNotIn("dnf remove", installer)
        self.assertNotIn("docker compose up", installer)

    def test_publish_gateway_waits_for_database_readiness(self):
        implementation = (ROOT / "deploy/venue-cloud/binhu-venue-publish-gateway.py").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:48727/health/ready", implementation)


if __name__ == "__main__":
    unittest.main()
