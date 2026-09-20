import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from staging.acceptance import assemble_report
from staging.control import load_manifest
from staging.evidence import merge_samples
from staging.fixture import validate_fixture
from staging.fixture_model import make_accounts, make_tasks, runtime_index
from staging.guard import StagingSafetyError, validate_staging_environment
from staging.metrics import StagingMetrics
from staging.report import evaluate_report
from staging.resource_metrics import summarize_resource_samples
from staging.stream_clients import StreamHooks, cookie_header, run_query_websocket, run_sse, websocket_url
from staging.workload import load_runtime_index, retry_delay


RUN_ID = "STG-20260920-01"
DIGESTS = {
    "kafka": "sha256:" + "0" * 64,
    "schema_registry": "sha256:" + "5" * 64,
    "mysql": "sha256:" + "1" * 64,
    "redis": "sha256:" + "2" * 64,
    "worker": "sha256:" + "3" * 64,
    "flink": "sha256:" + "4" * 64,
}


def env():
    return {
        "APP_ENVIRONMENT": "staging",
        "STAGING_LOAD_TEST_RUN_ID": RUN_ID,
        "COMPOSE_PROJECT_NAME": "binhu-staging-load-stg-20260920-01",
        "STAGING_BASE_URL": "https://staging.example.test",
        "STAGING_DB_NAME": "StagingLoad_20260920_01",
        "STAGING_DB_PASSWORD": "fixture-only",
        "STAGING_LOAD_TEST_PASSWORD": "fixture-only",
    }


def runtime_payload():
    return {
        "run_id": RUN_ID,
        "environment": "staging",
        "fictional_only": True,
        "production_data": False,
        "accounts": [
            {"username": f"staging-load-{index:02d}@staging", "role": "member", "scope": "mine"}
            for index in range(1, 76)
        ],
        "tasks": [{"parser_type": "全链条", "row_key": "synthetic:1", "scenario": "assigned"}],
        "property_search_keyword": "演练路",
    }


class StagingLoadTests(unittest.TestCase):
    def test_staging_fixture_model_is_fictional_credential_free_and_large_enough(self):
        accounts = make_accounts()
        tasks = make_tasks()
        self.assertEqual(len(accounts), 75)
        self.assertEqual(len({item["username"] for item in accounts}), 75)
        self.assertTrue(all(item["username"].endswith("@staging") for item in accounts))
        self.assertEqual(len(tasks), 1440)
        self.assertEqual({item["scenario"] for item in tasks}, {"assigned", "unassigned", "assignable"})
        payload = runtime_index(RUN_ID, [{
            "parser_type": "全链条", "row_key": "synthetic", "scenario": "assigned",
        }])
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("password", serialized)
        self.assertNotIn("secret", serialized)
        self.assertFalse(payload["production_data"])

    def test_staging_seeder_uses_app_source_and_all_database_markers(self):
        source = (Path(__file__).parent / "staging" / "seed.py").read_text(encoding="utf-8")
        self.assertIn("create_local_source_row", source)
        self.assertIn("source_kind=\"local_table\"", source)
        self.assertIn("staging:{run_id}:task:", source)
        self.assertIn("_environment_identity", source)
        self.assertIn("--password-stdin", source)
        self.assertNotIn("password_hash\":", source)

    def test_resource_summary_fails_closed_when_any_layer_is_missing(self):
        result = summarize_resource_samples([
            {"mysql": {}, "redis": {}, "kafka": {}, "flink": {}, "derived_queue": {}, "production": None},
            {"mysql": {}, "redis": {}, "kafka": {}, "flink": {}, "derived_queue": {}, "production": None},
        ])
        self.assertTrue(result["unverified"])
        self.assertIn("sample[0].production", result["missing"])

    def test_production_samples_are_merged_only_from_readonly_aligned_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging.jsonl"
            production = root / "production.jsonl"
            stage_rows = [
                {"run_id": RUN_ID, "environment": "staging", "production_data": False,
                 "sampled_at_unix": 100 + index, "production": None}
                for index in range(2)
            ]
            prod_rows = [
                {"environment": "production", "read_only": True,
                 "business_data_included": False, "sampled_at_unix": 100 + index,
                 "metrics": {"healthy": True, "restart_count": 0,
                             "oom_killed_count": 0, "error_count": 0}}
                for index in range(2)
            ]
            staging.write_text("\n".join(json.dumps(x) for x in stage_rows), encoding="utf-8")
            production.write_text("\n".join(json.dumps(x) for x in prod_rows), encoding="utf-8")
            merged = merge_samples(RUN_ID, staging, production)
            self.assertTrue(merged["samples"][0]["production"]["healthy"])
            prod_rows[0]["read_only"] = False
            production.write_text("\n".join(json.dumps(x) for x in prod_rows), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                merge_samples(RUN_ID, staging, production)

    def test_guard_accepts_only_exact_staging_contract(self):
        context = validate_staging_environment(RUN_ID, env())
        self.assertEqual(context.project, "binhu-staging-load-stg-20260920-01")
        for key, value in (
            ("APP_ENVIRONMENT", "production"),
            ("STAGING_BASE_URL", "http://staging.example.test"),
            ("STAGING_DB_NAME", "OnlineData"),
            ("STAGING_LOAD_TEST_PASSWORD", ""),
        ):
            candidate = env(); candidate[key] = value
            with self.assertRaises(StagingSafetyError):
                validate_staging_environment(RUN_ID, candidate)

    def test_runtime_index_requires_75_staging_accounts_and_deidentified_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps(runtime_payload(), ensure_ascii=False), encoding="utf-8")
            runtime = load_runtime_index(str(path), RUN_ID)
            self.assertEqual(len(runtime.accounts), 75)
            invalid = runtime_payload(); invalid["accounts"][0]["username"] = "production-user"
            path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_runtime_index(str(path), RUN_ID)

    def test_backoff_is_exponential_bounded_and_jittered(self):
        self.assertEqual(retry_delay(1, 1, maximum_seconds=30, jitter_ratio=.25, random_value=lambda: .5), 1)
        self.assertEqual(retry_delay(1, 5, maximum_seconds=10, jitter_ratio=.25, random_value=lambda: .5), 10)
        self.assertLess(retry_delay(1, 3, maximum_seconds=30, random_value=lambda: 0), 4)
        self.assertGreater(retry_delay(1, 3, maximum_seconds=30, random_value=lambda: 1), 4)

    def test_stream_urls_are_fixed_to_staging_prefix_and_cookies_are_not_logged(self):
        self.assertEqual(
            websocket_url("https://staging.example.test", "/staging/api", "全链条"),
            "wss://staging.example.test/staging/api/query/live/%E5%85%A8%E9%93%BE%E6%9D%A1",
        )
        self.assertEqual(cookie_header({"binhu_staging_session": "opaque"}), "binhu_staging_session=opaque")
        with self.assertRaises(ValueError):
            websocket_url("http://production.example.test", "/api", "全链条")

    def test_failed_sse_reconnect_does_not_decrement_an_unopened_connection(self):
        calls = {"wait": 0, "opened": 0, "closed": 0, "reconnecting": 0}

        class Stop:
            def is_set(self):
                return False

            def wait(self, timeout):
                del timeout
                calls["wait"] += 1
                return calls["wait"] >= 2

        class Response:
            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=True):
                del decode_unicode
                return []

            def close(self):
                return None

        hooks = StreamHooks(
            opened=lambda *_: calls.__setitem__("opened", calls["opened"] + 1),
            closed=lambda *_: calls.__setitem__("closed", calls["closed"] + 1),
            reconnecting=lambda *_: calls.__setitem__("reconnecting", calls["reconnecting"] + 1),
            failed=lambda *_: None,
        )
        with patch("requests.get", side_effect=[Response(), OSError("synthetic")]):
            run_sse(
                base_url="https://staging.example.test", api_prefix="/staging/api",
                cookies={}, stop=Stop(), hooks=hooks, forced_disconnect_seconds=0,
            )
        self.assertEqual(calls["opened"], 1)
        self.assertEqual(calls["closed"], 1)
        self.assertEqual(calls["reconnecting"], 1)

    def test_failed_websocket_reconnect_does_not_decrement_an_unopened_connection(self):
        calls = {"wait": 0, "opened": 0, "closed": 0, "reconnecting": 0}

        class Stop:
            def is_set(self):
                return False

            def wait(self, timeout):
                del timeout
                calls["wait"] += 1
                return calls["wait"] >= 2

        class Socket:
            def recv(self):
                return ""

            def close(self):
                return None

        hooks = StreamHooks(
            opened=lambda *_: calls.__setitem__("opened", calls["opened"] + 1),
            closed=lambda *_: calls.__setitem__("closed", calls["closed"] + 1),
            reconnecting=lambda *_: calls.__setitem__("reconnecting", calls["reconnecting"] + 1),
            failed=lambda *_: None,
        )
        websocket_module = SimpleNamespace(
            create_connection=Mock(side_effect=[Socket(), OSError("synthetic")]),
        )
        with patch.dict(sys.modules, {"websocket": websocket_module}):
            run_query_websocket(
                base_url="https://staging.example.test", api_prefix="/staging/api",
                parser_type="全链条", cookies={}, stop=Stop(), hooks=hooks,
                forced_disconnect_seconds=0,
            )
        self.assertEqual(calls["opened"], 1)
        self.assertEqual(calls["closed"], 1)
        self.assertEqual(calls["reconnecting"], 1)

    def test_report_keeps_layers_separate_and_tracks_reconnect_peak(self):
        metrics = StagingMetrics()
        metrics.observe("core.save", 120, False)
        metrics.observe("poll.heartbeat", 40, False)
        metrics.observe("poll.heartbeat", 2200, True)
        metrics.stream_opened(reconnect=False)
        metrics.stream_reconnect_started(0)
        metrics.stream_opened(reconnect=True)
        report = metrics.report()
        self.assertEqual(report["layers"]["core.save"]["p95_ms"], 120)
        self.assertEqual(report["layers"]["poll.heartbeat"]["failures"], 1)
        self.assertEqual(report["events"]["reconnect_peak_per_minute"], 1)
        self.assertEqual(report["events"]["reconnect_success_rate"], 1)

    def test_resource_summary_records_all_required_runtime_layers(self):
        samples = [
            {
                "mysql": {"Threads_connected": 10, "Threads_running": 2, "Innodb_row_lock_current_waits": 0,
                          "Innodb_row_lock_time_max": 100, "Innodb_deadlocks": 3},
                "backend_pool": {"pool_count": 8, "usage_ratio": .5, "used": 20, "max_size": 40},
                "redis": {"used_memory": 50, "maxmemory": 100, "oom_error_count": 0,
                          "evicted_keys": 0, "keyspace_hits": 100, "keyspace_misses": 10},
                "kafka": {"lag": 5},
                "flink": {"checkpoint_completed": 10, "checkpoint_failed": 1,
                          "checkpoint_duration_ms": 1000, "backpressure_ratio": 0},
                "derived_queue": {"pending": 4},
                "production": {"healthy": True, "restart_count": 2, "oom_killed_count": 0, "error_count": 7},
            },
            {
                "mysql": {"Threads_connected": 12, "Threads_running": 3, "Innodb_row_lock_current_waits": 0,
                          "Innodb_row_lock_time_max": 200, "Innodb_deadlocks": 3},
                "backend_pool": {"pool_count": 8, "usage_ratio": .75, "used": 30, "max_size": 40},
                "redis": {"used_memory": 60, "maxmemory": 100, "oom_error_count": 0,
                          "evicted_keys": 0, "keyspace_hits": 200, "keyspace_misses": 20},
                "kafka": {"lag": 0},
                "flink": {"checkpoint_completed": 12, "checkpoint_failed": 1,
                          "checkpoint_duration_ms": 1500, "backpressure_ratio": .1},
                "derived_queue": {"pending": 0, "drain_seconds": 20},
                "production": {"healthy": True, "restart_count": 2, "oom_killed_count": 0, "error_count": 7},
            },
        ]
        result = summarize_resource_samples(samples)
        self.assertEqual(result["mysql"]["deadlock_delta"], 0)
        self.assertEqual(result["mysql"]["pool_usage_ratio_peak"], .75)
        self.assertEqual(result["kafka"]["lag_final"], 0)
        self.assertEqual(result["flink"]["checkpoint_completed_delta"], 2)
        self.assertEqual(result["derived_queue"]["drain_seconds"], 20)
        self.assertTrue(result["production"]["healthy_all_samples"])

    def test_model_uses_timed_pollers_long_streams_and_current_write_contract(self):
        source = (Path(__file__).parent / "staging" / "locustfile.py").read_text(encoding="utf-8")
        for marker in (
            "self._spawn_poll(30, self._heartbeat)", "self._spawn_poll(30, self._unread)",
            "self._spawn_poll(60, self._identity)", "self._spawn_poll(30, self._query_data)",
            "self._spawn_poll(15, self._query_version)", 'name="poll.query_data"',
            "run_sse", "run_query_websocket", "source-rows/{source_id}", "bulk-assign",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("@task(6)\n    def polling_heartbeat", source)
        self.assertIn('API_PREFIX = "/staging/api"', source)

    def test_report_marks_missing_resource_and_queue_gates_unverified(self):
        report = StagingMetrics().report()
        result = evaluate_report(report)
        self.assertFalse(result["passed"])
        self.assertIn("layers.core.save.p95_ms", result["unverified"])
        self.assertIn("resources.mysql.deadlock_delta", result["unverified"])

    def test_fixture_requires_deidentified_relations(self):
        result = validate_fixture({
            "run_id": RUN_ID, "environment": "staging", "fictional_only": True,
            "production_data": False, "relations": {"users": 75, "tasks": 674},
        })
        self.assertEqual(result["sensitive_matches"], 0)

    def test_manifest_requires_immutable_candidate_and_staging_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            manifest = {
                "run_id": RUN_ID, "environment": "staging", "fictional_only": True,
                "production_data": False, "candidate_image_digest": "sha256:" + "a" * 64,
                "commit": "b" * 40,
                "configuration_sha256": "c" * 64,
                "staging_snapshot_id": "staging-" + "d" * 16,
                "event_pipeline_image_digests": DIGESTS,
                "dev_acceptance": {
                    "status": "passed",
                    "run_id": "dev-20260919-dualtrack-monitor40",
                    "candidate_image_digest": "sha256:" + "a" * 64,
                    "event_pipeline_image_digests": DIGESTS,
                },
            }
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(load_manifest(str(path), RUN_ID)["run_id"], RUN_ID)

            for mutation in (
                lambda value: value.update(candidate_image_digest="sha256:short"),
                lambda value: value["dev_acceptance"].update(status="pending"),
                lambda value: value["dev_acceptance"].update(candidate_image_digest="sha256:" + "f" * 64),
                lambda value: value.update(staging_snapshot_id="staging-old"),
                lambda value: value["event_pipeline_image_digests"].pop("flink"),
            ):
                candidate = json.loads(json.dumps(manifest))
                mutation(candidate)
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    load_manifest(str(path), RUN_ID)

    def test_final_report_requires_load_resources_rollback_and_manual_acceptance(self):
        load = {
            "run_id": RUN_ID, "environment": "staging", "production_data": False,
            "layers": {
                "core.save": {"p95_ms": 1000},
                "poll.heartbeat": {"p95_ms": 100, "success_rate": 1.0},
                "poll.unread": {"p95_ms": 100, "success_rate": 1.0},
                "poll.maintenance": {"p95_ms": 100, "success_rate": 1.0},
                "poll.query_data": {"p95_ms": 800, "success_rate": 1.0},
            },
            "core": {"success_rate": 1.0},
            "events": {"reconnect_success_rate": 1.0, "reconnect_peak_per_minute": 2},
        }
        first = {
            "mysql": {"Threads_connected": 4, "Threads_running": 1, "Innodb_row_lock_current_waits": 0,
                      "Innodb_row_lock_time_max": 10, "Innodb_deadlocks": 0},
            "backend_pool": {"pool_count": 8, "usage_ratio": .5, "used": 20, "max_size": 40},
            "redis": {"used_memory": 50, "maxmemory": 100, "oom_error_count": 0,
                      "evicted_keys": 0, "keyspace_hits": 10, "keyspace_misses": 1},
            "kafka": {"lag": 1},
            "flink": {"checkpoint_completed": 2, "checkpoint_failed": 0,
                      "checkpoint_duration_ms": 500, "backpressure_ratio": 0},
            "derived_queue": {"pending": 1},
            "production": {"healthy": True, "restart_count": 0, "oom_killed_count": 0, "error_count": 0},
        }
        second = json.loads(json.dumps(first))
        second["kafka"]["lag"] = 0
        second["flink"]["checkpoint_completed"] = 3
        second["derived_queue"] = {"pending": 0, "drain_seconds": 20}
        envelope = {"run_id": RUN_ID, "environment": "staging", "production_data": False,
                    "samples": [first, second]}
        rollback = {"run_id": RUN_ID, "environment": "staging", "production_data": False,
                    "status": "passed", "python_worker_restored": True,
                    "data_consistency_verified": True, "legacy_path_healthy": True,
                    "elapsed_seconds": 12.5}
        manual = {"run_id": RUN_ID, "environment": "staging", "production_data": False,
                  "status": "passed", "checks": {"login": True, "query": True, "cookie_isolation": True}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, payload in (("load", load), ("resources", envelope),
                                  ("rollback", rollback), ("manual", manual)):
                (root / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
            report = assemble_report(
                run_id=RUN_ID,
                load_report_path=root / "load.json",
                resource_samples_path=root / "resources.json",
                rollback_report_path=root / "rollback.json",
                manual_report_path=root / "manual.json",
            )
            self.assertTrue(report["passed"])
            rollback["data_consistency_verified"] = False
            (root / "rollback.json").write_text(json.dumps(rollback), encoding="utf-8")
            self.assertFalse(assemble_report(
                run_id=RUN_ID,
                load_report_path=root / "load.json",
                resource_samples_path=root / "resources.json",
                rollback_report_path=root / "rollback.json",
                manual_report_path=root / "manual.json",
            )["passed"])


if __name__ == "__main__":
    unittest.main()
