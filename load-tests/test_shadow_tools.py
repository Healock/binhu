import csv
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from fixture import BUSINESS_TYPES, make_tasks, make_users, write_manifest
from metrics import summarize
from shadow_guard import ShadowSafetyError, validate_shadow_environment
from shadowctl import (
    _conflict_groups,
    _latest_successful_fields,
    _validate_https_origin,
    _validate_production_health_url,
    _validate_run_shape,
    _verify_pinned_images,
    _verify_production_proof,
)


RUN_ID = "LT-20260902-01"


def shadow_env() -> dict[str, str]:
    return {
        "APP_ENVIRONMENT": "shadow",
        "LOAD_TEST_RUN_ID": RUN_ID,
        "COMPOSE_PROJECT_NAME": "binhu-loadtest-lt-20260902-01",
        "SHADOW_DB_HOST": "127.0.0.1",
        "SHADOW_DB_PORT": "47126",
        "SHADOW_DB_NAME": "LoadTest_LT_20260902_01",
    }


class ShadowToolTests(unittest.TestCase):
    def test_fixture_counts_and_role_numbering_match_plan(self):
        users = make_users()
        self.assertEqual(len(users), 76)
        self.assertEqual(len({str(user["username"]) for user in users}), 76)
        self.assertEqual(len({str(user["display_name"]) for user in users}), 76)
        roles = Counter(str(user["role"]) for user in users if user["username"] != "observer@shadow")
        self.assertEqual(
            roles,
            {"member": 60, "leader": 8, "internal_business": 4,
             "admin": 2, "super_admin": 1},
        )
        self.assertIn("loadtest-member-01", {user["username"] for user in users})
        self.assertIn("loadtest-leader-01", {user["username"] for user in users})

    def test_task_distribution_writers_and_conflicts(self):
        tasks = make_tasks()
        self.assertEqual(len(tasks), 3600)
        by_parser = Counter(str(task["parser_type"]) for task in tasks)
        self.assertEqual(by_parser, {parser_type: 600 for parser_type in BUSINESS_TYPES})
        for parser_type in BUSINESS_TYPES:
            states = Counter(
                str(task["state"])
                for task in tasks if task["parser_type"] == parser_type
            )
            self.assertEqual(states, {
                "assigned": 450, "unassigned": 60, "pending_registration": 30,
                "unverifiable": 30, "completed": 30,
            })
        self.assertEqual(sum(bool(task["conflict_group"]) for task in tasks), 30)
        self.assertTrue(all(
            bool(task["assigned_user"]) == (task["state"] != "unassigned")
            for task in tasks
        ))
        assigned_usernames = {
            str(task["assigned_username"]) for task in tasks if task["assigned_username"]
        }
        self.assertTrue({f"burst-{index:02d}" for index in range(1, 26)} <= assigned_usernames)
        for task in tasks:
            property_index = int(task["property_index"])
            property_community = (property_index - 1) // 4 + 1
            self.assertEqual(task["community"], f"压测社区{property_community:02d}")

    def test_manifest_is_explicitly_fictional(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_manifest(Path(directory), RUN_ID)
            content = path.read_text(encoding="utf-8")
            self.assertIn('"fictional_only": true', content)
            self.assertNotIn("password", content.lower())
            manifest = __import__("json").loads(content)
            self.assertTrue(all(
                len(community["properties"]) == 4
                for community in manifest["communities"]
            ))

    def test_shadow_database_bootstrap_includes_legacy_sync_table(self):
        bootstrap = (Path(__file__).parent / "shadow-marker.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("CREATE TABLE IF NOT EXISTS _shadow_loadtest_marker", bootstrap)
        self.assertIn("CREATE TABLE IF NOT EXISTS _sync_log", bootstrap)
        self.assertIn("VALUES ('__UNSEEDED__', 'shadow')", bootstrap)

    def test_shadow_database_bootstrap_loads_complete_local_schema(self):
        root = Path(__file__).parent
        compose = (root / "docker-compose.shadow.yml").read_text(encoding="utf-8")
        bootstrap = (root / "shadow-schema-bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("./shadow-schema-bootstrap.sh:/docker-entrypoint-initdb.d/01-shadow-schema-bootstrap.sh:ro", compose)
        self.assertIn("../backend/init.sql:/shadow-schema/backend-init.sql:ro", compose)
        self.assertIn('^LoadTest_[A-Za-z0-9_]+$', bootstrap)
        self.assertIn("mysql --protocol=socket", bootstrap)
        self.assertIn("ALTER DATABASE", bootstrap)
        self.assertIn("utf8mb4_unicode_ci", bootstrap)
        self.assertIn("s/OnlineData/", bootstrap)

    def test_runtime_index_joins_use_explicit_shadow_collation(self):
        source = (Path(__file__).parent / "shadowctl.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            source.count("projection.parser_type COLLATE utf8mb4_unicode_ci"),
            3,
        )
        self.assertGreaterEqual(
            source.count("projection.row_key COLLATE utf8mb4_unicode_ci"),
            3,
        )

    def test_guard_rejects_wrong_environment_project_and_port(self):
        env = shadow_env()
        env["APP_ENVIRONMENT"] = "production"
        with self.assertRaises(ShadowSafetyError):
            validate_shadow_environment(RUN_ID, env)
        env = shadow_env()
        env["COMPOSE_PROJECT_NAME"] = "binhu"
        with self.assertRaises(ShadowSafetyError):
            validate_shadow_environment(RUN_ID, env)
        env = shadow_env()
        env["SHADOW_DB_PORT"] = "3306"
        with self.assertRaises(ShadowSafetyError):
            validate_shadow_environment(RUN_ID, env)

    def test_guard_accepts_exact_loopback_shadow_target(self):
        context = validate_shadow_environment(RUN_ID, shadow_env())
        self.assertEqual(context.project, "binhu-loadtest-lt-20260902-01")
        self.assertEqual(context.db_host, "127.0.0.1")
        self.assertEqual(context.db_port, 47126)

    def test_api_origin_does_not_accept_paths_or_credentials(self):
        self.assertEqual(
            _validate_https_origin("https://example.test/", label="test"),
            "https://example.test",
        )
        for value in (
            "http://example.test", "https://example.test/shadow-api",
            "https://user:pass@example.test", "https://example.test/?next=x",
        ):
            with self.assertRaises(ShadowSafetyError):
                _validate_https_origin(value, label="test")

    def test_production_health_requires_exact_https_health_path(self):
        self.assertEqual(
            _validate_production_health_url("https://example.test/api/health"),
            "https://example.test/api/health",
        )
        for value in (
            "http://example.test/api/health", "https://example.test",
            "https://example.test/api/health?full=1", "https://example.test/health",
        ):
            with self.assertRaises(ShadowSafetyError):
                _validate_production_health_url(value)

    def test_all_images_must_be_digest_pinned(self):
        digest = "sha256:" + "a" * 64
        with patch.dict(os.environ, {
            "SHADOW_BACKEND_IMAGE": digest,
            "SHADOW_MYSQL_IMAGE": f"mysql@{digest}",
            "SHADOW_REDIS_IMAGE": f"redis@{digest}",
        }, clear=False):
            _verify_pinned_images()
        with patch.dict(os.environ, {
            "SHADOW_BACKEND_IMAGE": "backend:latest",
            "SHADOW_MYSQL_IMAGE": f"mysql@{digest}",
            "SHADOW_REDIS_IMAGE": f"redis@{digest}",
        }, clear=False):
            with self.assertRaises(ShadowSafetyError):
                _verify_pinned_images()

    def test_locust_contract_uses_shadow_prefix_post_claim_and_registration(self):
        source = (Path(__file__).parent / "locustfile.py").read_text(encoding="utf-8")
        self.assertIn('API_PREFIX = "/shadow-api"', source)
        self.assertIn("method = self.client.post if claim else self.client.patch", source)
        self.assertIn('body["registration_property_id"]', source)
        self.assertIn('body["registration_property_version"]', source)
        self.assertIn('target.get("property_candidates")', source)
        self.assertIn('"kind": "claim" if claim else "write"', source)
        self.assertIn("response.success()", source)

    def test_expected_409_is_excluded_and_latest_success_uses_revision_order(self):
        events = [
            {"status": 200, "source_id": 7, "returned_revision": 3, "at": 30,
             "changes": {"备注": "revision-3"}},
            {"status": 409, "source_id": 7, "returned_revision": 0, "at": 50,
             "changes": {"备注": "must-not-win"}},
            {"status": 200, "source_id": 7, "returned_revision": 2, "at": 60,
             "changes": {"备注": "late-response-old-revision"}},
        ]
        latest = _latest_successful_fields(events)
        self.assertEqual(latest[(7, "备注")][1], "revision-3")

    def test_conflict_events_group_by_pair_source_and_revision(self):
        events = [
            {"kind": "conflict", "pair": 1, "source_id": 7, "read_revision": 2,
             "status": 200},
            {"kind": "conflict", "pair": 1, "source_id": 7, "read_revision": 2,
             "status": 409},
            {"kind": "write", "source_id": 7, "status": 200},
        ]
        groups = _conflict_groups(events)
        self.assertEqual(list(groups), [(1, 7, 2)])
        self.assertEqual(sorted(item["status"] for item in groups[(1, 7, 2)]), [200, 409])

    def test_run_shape_rejects_wrong_user_count_or_duration(self):
        _validate_run_shape("login", 50, "5m")
        _validate_run_shape("mixed", 75, "5m")
        _validate_run_shape("conflict", 20, "10m")
        with self.assertRaises(ShadowSafetyError):
            _validate_run_shape("conflict", 50, "10m")
        with self.assertRaises(ShadowSafetyError):
            _validate_run_shape("mixed", 50, "0m")

    def test_production_zero_data_proof_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proof.json"
            path.write_text(
                '{"run_id":"LT-20260902-01","matching_rows":0,'
                '"checked_at":"2026-09-02T12:00:00Z",'
                '"checked_scopes":["users","tasks"]}',
                encoding="utf-8",
            )
            result = _verify_production_proof(path, RUN_ID)
            self.assertTrue(result["provided"])
            path.write_text(
                '{"run_id":"LT-20260902-01","matching_rows":1,'
                '"checked_at":"2026-09-02T12:00:00Z",'
                '"checked_scopes":["tasks"]}',
                encoding="utf-8",
            )
            with self.assertRaises(ShadowSafetyError):
                _verify_production_proof(path, RUN_ID)

    def test_concentrated_login_defines_exactly_fifty_core_accounts(self):
        namespace: dict[str, object] = {}
        source = (Path(__file__).parent / "login_locust.py").read_text(encoding="utf-8")
        self.assertIn("self.index % 50", source)
        self.assertIn('"member", 35', source)
        self.assertIn('"leader", 8', source)
        self.assertNotIn("burst-", source)

    def test_metrics_summary_excludes_aggregated_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stats.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["Name", "Request Count", "Failure Count"])
                writer.writeheader()
                writer.writerow({"Name": "one", "Request Count": 10, "Failure Count": 1})
                writer.writerow({"Name": "Aggregated", "Request Count": 10, "Failure Count": 1})
            result = summarize(path)
            self.assertEqual(result["requests"], 10)
            self.assertEqual(result["failures"], 1)


if __name__ == "__main__":
    unittest.main()
