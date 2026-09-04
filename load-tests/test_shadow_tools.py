import csv
import os
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from fixture import BUSINESS_TYPES, make_tasks, make_users, write_manifest
from metrics import (
    _production_container_states,
    projection_stop_reason,
    requests_stalled,
    summarize,
)
from locustfile import (
    FlowUser,
    _barrier_for_pair,
    _conflict_barriers,
    _conflict_tasks,
)
from shadow_guard import ShadowSafetyError, validate_shadow_environment
from shadowctl import (
    _conflict_groups,
    _latest_successful_fields,
    _successful_revisions,
    _classify_field_verification,
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
    def test_conflict_scenario_uses_only_coordinated_task(self):
        self.assertEqual(_conflict_tasks(FlowUser), [FlowUser.concurrent_conflict])

    def test_broken_conflict_barrier_is_replaced(self):
        pair_index = 999
        broken = threading.Barrier(2)
        broken.abort()
        _conflict_barriers[pair_index] = broken
        replacement = _barrier_for_pair(pair_index)
        self.assertIsNot(replacement, broken)
        self.assertFalse(replacement.broken)
        _conflict_barriers.pop(pair_index, None)

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
        self.assertIn("awk '/^-- daily_report", bootstrap)
        self.assertIn("GRANT ALL PRIVILEGES", bootstrap)
        self.assertEqual(bootstrap.count('initialize_online_database'), 2)
        self.assertIn('^LoadTest_[A-Za-z0-9_]+$', bootstrap)
        self.assertIn("mysql --protocol=socket", bootstrap)
        self.assertIn("ALTER DATABASE", bootstrap)
        self.assertIn("utf8mb4_unicode_ci", bootstrap)
        self.assertIn("s/OnlineData/", bootstrap)
        self.assertIn("DROP INDEX uk_row_key", bootstrap)
        self.assertIn("MySQL 1091", bootstrap)
        self.assertIn("not safe during a fresh shadow bootstrap", bootstrap)
        self.assertIn("DROP INDEX uk_row_key//g", bootstrap)

    def test_bootstrap_does_not_leak_nounset_into_mysql_entrypoint(self):
        bootstrap = (Path(__file__).parent / "shadow-schema-bootstrap.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("set -eo pipefail", bootstrap)
        self.assertNotIn("set -euo pipefail", bootstrap)

    def test_shadow_compose_supports_desktop_sessions_and_resource_monitoring(self):
        compose = (Path(__file__).parent / "docker-compose.shadow.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("SESSION_COOKIE_SAMESITE: none", compose)
        self.assertIn("http://tauri.localhost", compose)
        self.assertIn("OPS_AGENT_URL: http://ops-agent:9001", compose)
        self.assertIn("SHADOW_DAILY_DB_NAME", compose)
        self.assertIn('DAILY_DOMAIN_ACTIVE: "true"', compose)
        self.assertIn("SHADOW_OPS_AGENT_TOKEN", compose)
        self.assertIn("OPS_AGENT_CONTAINERS:", compose)
        self.assertIn("${COMPOSE_PROJECT_NAME}-backend-1", compose)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", compose)
        self.assertIn('ONLINE_PROJECTION_WORKER_CONCURRENCY: "4"', compose)

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

    def test_seed_uses_active_local_source_kind_with_run_scoped_reference(self):
        seed = (Path(__file__).parent / "seed_shadow.py").read_text(encoding="utf-8")
        controller = (Path(__file__).parent / "shadowctl.py").read_text(encoding="utf-8")
        self.assertIn('source_kind="local_table"', seed)
        self.assertNotIn('source_kind="shadow_loadtest"', seed)
        self.assertIn('f"shadow:{run_id}:task:', seed)
        self.assertIn("source_kind='local_table'", controller)
        self.assertIn("spreadsheet_id=0", controller)

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
        self.assertIn("if not eligible:", source)
        self.assertLess(
            source.index('range(31, 36)'),
            source.index('range(1, 31)'),
        )

    def test_runner_waits_for_in_flight_requests_before_exit(self):
        source = (Path(__file__).parent / "shadowctl.py").read_text(encoding="utf-8")
        self.assertIn('"--stop-timeout", "15"', source)

    def test_container_monitor_persists_only_safe_state_fields(self):
        inspect_payload = {
            "RestartCount": 2,
            "State": {
                "Running": True,
                "Restarting": False,
                "OOMKilled": False,
                "Health": {"Status": "healthy", "Log": [{"Output": "secret"}]},
            },
            "Config": {"Env": ["PASSWORD=must-not-be-recorded"]},
            "Mounts": [{"Source": "/private/path"}],
        }
        completed = __import__("subprocess").CompletedProcess(
            ["docker"], 0, stdout=__import__("json").dumps(inspect_payload), stderr=""
        )
        with patch("metrics.subprocess.run", return_value=completed):
            states = _production_container_states(("binhu-backend",))
        self.assertEqual(states["binhu-backend"], {
            "running": True,
            "restarting": False,
            "oom_killed": False,
            "health": "healthy",
            "restart_count": 2,
        })
        self.assertNotIn("PASSWORD", __import__("json").dumps(states))

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

    def test_later_committed_revision_wins_even_when_response_arrives_first(self):
        events = [
            {"status": 200, "source_id": 7, "returned_revision": 5, "at": 10,
             "operation_id": "op-new", "changes": {"备注": "revision-5"}},
            {"status": 200, "source_id": 7, "returned_revision": 4, "at": 20,
             "operation_id": "op-old", "changes": {"备注": "revision-4"}},
            {"status": 503, "source_id": 7, "returned_revision": 99, "at": 30,
             "failed_operation_id": "op-failed", "changes": {"备注": "failed"}},
        ]
        latest = _latest_successful_fields(events)
        self.assertEqual(latest[(7, "备注")][0][0], 5)
        self.assertEqual(latest[(7, "备注")][1], "revision-5")

    def test_successful_revisions_ignore_conflicts_and_failures(self):
        events = [
            {"status": 200, "source_id": 7, "returned_revision": 3},
            {"status": 409, "source_id": 7, "returned_revision": 4},
            {"status": 503, "source_id": 7, "returned_revision": 5},
            {"status": 200, "source_id": 7, "returned_revision": 6},
        ]
        self.assertEqual(_successful_revisions(events), {7: {3, 6}})

    def test_field_verification_distinguishes_superseded_and_unrecorded(self):
        common = {
            "actual_revision": 7, "actual_value": "new",
            "event_revision": 6, "expected_value": "old", "field": "备注",
        }
        self.assertEqual(
            _classify_field_verification(
                **common, successful_revisions={7},
                successful_operations={"op-7"},
                audit_after_values=[("op-7", {"备注": "new"})],
            ),
            "superseded",
        )
        self.assertEqual(
            _classify_field_verification(
                **common, successful_revisions=set(),
                successful_operations=set(), audit_after_values=[],
            ),
            "unrecorded",
        )

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
                '"checked_scopes":["shadow_source_refs","shadow_usernames",'
                '"loadtest_prefixes","legacy_shadow_source_kind"],'
                '"scope_counts":{"shadow_source_refs":0,"shadow_usernames":0,'
                '"loadtest_prefixes":0,"legacy_shadow_source_kind":0}}',
                encoding="utf-8",
            )
            result = _verify_production_proof(path, RUN_ID)
            self.assertTrue(result["provided"])
            path.write_text(
                '{"run_id":"LT-20260902-01","matching_rows":1,'
                '"checked_at":"2026-09-02T12:00:00Z",'
                '"checked_scopes":["shadow_source_refs","shadow_usernames",'
                '"loadtest_prefixes","legacy_shadow_source_kind"],'
                '"scope_counts":{"shadow_source_refs":1,"shadow_usernames":0,'
                '"loadtest_prefixes":0,"legacy_shadow_source_kind":0}}',
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
        self.assertIn("def on_start", source)
        self.assertIn("def hold_session", source)
        self.assertNotIn("StopUser", source)
        self.assertNotIn("self.stop(True)", source)

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

    def test_projection_worker_stall_and_failure_stop_the_stage(self):
        self.assertEqual(
            projection_stop_reason({"projection_oldest_running_seconds": 31}),
            "shadow_projection_job_stalled_above_30_seconds",
        )
        self.assertEqual(
            projection_stop_reason({"projection_failed": 1}),
            "shadow_projection_job_failed",
        )
        self.assertEqual(
            projection_stop_reason({"projection_oldest_running_seconds": 30}),
            "",
        )

    def test_request_plateau_stops_business_traffic_but_not_login_hold(self):
        self.assertTrue(requests_stalled(
            scenario="mixed", request_count=100, last_progress_at=10, now=41,
        ))
        self.assertFalse(requests_stalled(
            scenario="mixed", request_count=99, last_progress_at=10, now=50,
        ))
        self.assertFalse(requests_stalled(
            scenario="login", request_count=50, last_progress_at=10, now=500,
        ))


if __name__ == "__main__":
    unittest.main()
