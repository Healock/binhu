import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class EnvironmentGatewayNginxContractTests(unittest.TestCase):
    def setUp(self):
        self.template = (ROOT / "nginx/migration/environment-account-gateway.conf").read_text(
            encoding="utf-8"
        )
        self.production = (ROOT / "nginx/migration/new-production.conf").read_text(
            encoding="utf-8"
        )

    def test_internal_listener_and_exact_routes(self):
        self.assertIn("listen 172.18.0.1:18081;", self.template)
        self.assertNotRegex(self.template, r"listen\s+(?:0\.0\.0\.0|\[::\]|443)")
        for path in (
            "/development/api/internal/environment-accounts",
            "/development/api/internal/environment-accounts/reset",
            "/staging/api/internal/environment-accounts",
            "/staging/api/internal/environment-accounts/reset",
        ):
            self.assertIn(f"location = {path}", self.template)
        self.assertNotIn("location /development/", self.template)
        self.assertNotIn("location /staging/", self.template)

    def test_upstreams_and_method_boundaries_are_isolated(self):
        development = self.template.split("location = /development", 1)[1].split(
            "location = /staging", 1
        )[0]
        staging = self.template.split("location = /staging", 1)[1]
        self.assertIn("127.0.0.1:48125", development)
        self.assertNotIn("48126", development)
        self.assertIn("127.0.0.1:48126", staging)
        self.assertNotIn("48125", staging)
        self.assertIn("limit_except GET", self.template)
        self.assertIn("limit_except POST", self.template)
        self.assertIn("if ($request_method != GET) { return 405; }", self.template)
        self.assertIn("if ($request_method != POST) { return 405; }", self.template)

    def test_sensitive_logging_and_safe_upstream_errors(self):
        self.assertIn("allow 172.18.0.0/16;", self.template)
        self.assertIn("deny all;", self.template)
        self.assertIn("access_log off;", self.template)
        self.assertNotIn("$request_body", self.template)
        self.assertNotIn("$http_authorization", self.template)
        self.assertIn("proxy_intercept_errors on;", self.template)
        self.assertIn("environment_account_gateway_unavailable", self.template)

    def test_new_production_includes_adapter_without_touching_public_routes(self):
        self.assertIn("binhu-environment-account-gateway.conf", self.production)
        self.assertNotIn("listen 172.18.0.1:18081", self.production)
        public_routes = (ROOT / "nginx/migration/new-app-locations.conf").read_text(
            encoding="utf-8"
        )
        for route in ("location /api/", "/shadow-api/"):
            self.assertIn(route, public_routes)
        self.assertNotIn("/development/api/internal/environment-accounts", public_routes)
        self.assertNotIn("/staging/api/internal/environment-accounts", public_routes)


if __name__ == "__main__":
    unittest.main()
