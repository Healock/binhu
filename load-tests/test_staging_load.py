import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))
from services.parsers import get_parser  # noqa: E402

from staging.acceptance import assemble_report
from staging import control as staging_control
from staging.control import load_manifest
from staging.evidence import merge_samples
from staging.fixture import validate_fixture
from staging.fixture_model import make_accounts, make_tasks, runtime_index
from staging.guard import StagingSafetyError, validate_staging_environment
from staging.metrics import StagingMetrics
from staging.load_monitor import MEMORY_GATE_BYTES, observe, preflight
from staging.production_guard import (
    PRODUCTION_MODE_PAUSED,
    ProductionStopMonitor,
)
from staging.profile import get_profile, profile_payload, select_accounts
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
    accounts = make_accounts()
    return {
        "run_id": RUN_ID,
        "environment": "staging",
        "fictional_only": True,
        "production_data": False,
        "accounts": [
            {"username": item["username"], "role": item["role"], "scope": item["scope"]}
            for item in accounts
        ],
        "tasks": [{"parser_type": "全链条", "row_key": "synthetic:1", "scenario": "assigned"}],
        "property_search_keyword": "演练路",
    }


class StagingLoadTests(unittest.TestCase):
    @staticmethod
    def _safety_sample(*, memory=3 * 1024**3, production_errors=0):
        names = ("binhu-backend", "binhu-mysql", "binhu-ops-agent")
        containers = {name: {"running": True, "restarting": False, "oom_killed": False,
                             "health": "healthy", "restart_count": 2} for name in names}
        return {
            "run_id": RUN_ID, "environment": "staging", "production_data": False,
            "host": {"memory_available_bytes": memory},
            "containers": {"count": 14, "running_count": 14, "restart_count": 0, "oom_killed_count": 0},
            "staging_services": {
                name: {"running": True, "restarting": False, "oom_killed": False, "restart_count": 1}
                for name in ("binhu-staging-backend-1", "binhu-staging-environment-mysql-1")
            },
            "production": {
                "environment": "production", "bootstrap_environment": "production",
                "bootstrap_status": 200, "health_status": 200, "health": True,
                "containers": containers, "error_count": production_errors, "monitor_error": None,
            },
        }

    def test_4gib_preflight_is_default_deny_and_explicit_waiver_is_auditable(self):
        sample = self._safety_sample(memory=MEMORY_GATE_BYTES - 1)
        with self.assertRaisesRegex(ValueError, "staging_memory_gate_not_waived"):
            preflight(sample, run_id=RUN_ID, waive_memory_gate=False)
        policy = preflight(sample, run_id=RUN_ID, waive_memory_gate=True)
        self.assertEqual(policy["memory_gate"], {
            "required_bytes": MEMORY_GATE_BYTES,
            "observed_bytes": MEMORY_GATE_BYTES - 1,
            "waiver_requested": True,
            "waived": True,
            "status": "waived",
        })

    def test_production_paused_mode_requires_explicit_pause_evidence_and_scope(self):
        sample = self._safety_sample()
        sample["production"] = {
            "environment": "production", "bootstrap_environment": "production",
            "bootstrap_status": 0, "health_status": 0, "health": False,
            "production_mode": PRODUCTION_MODE_PAUSED,
            "pause_run_id": "PROD-PAUSE-20261029-02",
            "pause_evidence_valid": True,
            "pause_evidence_sha256": "a" * 64,
            "backup_manifest_sha256": "b" * 64,
            "backup_database_count": 8,
            "backup_database_scope": [
                "DispatchData", "OnlineData", "OnlineDataArchive", "PlatformData",
                "RegistryData", "VisitData", "WorkflowData", "daily_report",
            ],
            "containers": {
                "binhu-backend": {"running": False, "restarting": False,
                                   "oom_killed": False, "health": "healthy", "restart_count": 2},
                "binhu-mysql": {"running": False, "restarting": False,
                                "oom_killed": False, "health": "healthy", "restart_count": 2},
                "binhu-ops-agent": {"running": True, "restarting": False,
                                     "oom_killed": False, "health": "healthy", "restart_count": 2},
            },
            "error_count": 0, "monitor_error": None,
        }
        policy = preflight(
            sample, run_id=RUN_ID, waive_memory_gate=True,
            production_mode=PRODUCTION_MODE_PAUSED,
            production_pause_run_id="PROD-PAUSE-20261029-02",
        )
        self.assertEqual(policy["production_mode"], PRODUCTION_MODE_PAUSED)
        with self.assertRaisesRegex(ValueError, "production_pause_run_id_invalid"):
            preflight(sample, run_id=RUN_ID, waive_memory_gate=True,
                      production_mode=PRODUCTION_MODE_PAUSED,
                      production_pause_run_id="PROD-PAUSE-invalid")
        with self.assertRaisesRegex(ValueError, "production_pause_run_id_unexpected"):
            preflight(sample, run_id=RUN_ID, waive_memory_gate=True,
                      production_pause_run_id="PROD-PAUSE-20261029-02")

    def test_production_paused_monitor_stops_on_state_or_evidence_change(self):
        sample = self._safety_sample()
        sample["production"] = {
            "environment": "production", "bootstrap_environment": "production",
            "production_mode": PRODUCTION_MODE_PAUSED,
            "pause_run_id": "PROD-PAUSE-20261029-02", "pause_evidence_valid": True,
            "pause_evidence_sha256": "a" * 64, "backup_manifest_sha256": "b" * 64,
            "backup_database_count": 8,
            "backup_database_scope": [
                "DispatchData", "OnlineData", "OnlineDataArchive", "PlatformData",
                "RegistryData", "VisitData", "WorkflowData", "daily_report",
            ],
            "containers": {
                "binhu-backend": {"running": False, "restarting": False,
                                   "oom_killed": False, "restart_count": 2},
                "binhu-mysql": {"running": False, "restarting": False,
                                "oom_killed": False, "restart_count": 2},
                "binhu-ops-agent": {"running": True, "restarting": False,
                                     "oom_killed": False, "restart_count": 2},
            },
            "error_count": 0, "monitor_error": None,
        }
        policy = preflight(sample, run_id=RUN_ID, waive_memory_gate=True,
                           production_mode=PRODUCTION_MODE_PAUSED,
                           production_pause_run_id="PROD-PAUSE-20261029-02")
        with tempfile.TemporaryDirectory() as directory:
            stats = Path(directory) / "locust_stats.csv"
            stats.write_text("Type,Name,Request Count,Failure Count\n", encoding="utf-8")
            changed = json.loads(json.dumps(sample))
            changed["production"]["containers"]["binhu-backend"]["running"] = True
            _, reason = observe(changed, policy, locust_stats=stats)
            self.assertEqual(reason, "production_paused_state_changed")
            changed = json.loads(json.dumps(sample))
            changed["production"]["pause_evidence_sha256"] = "c" * 64
            _, reason = observe(changed, policy, locust_stats=stats)
            self.assertEqual(reason, "production_pause_evidence_mismatch")

    def test_production_monitor_fails_closed_for_missing_identity_and_stops_on_restart_oom_or_error(self):
        sample = self._safety_sample()
        policy = preflight(sample, run_id=RUN_ID, waive_memory_gate=True)
        state = json.loads(json.dumps(policy))
        with tempfile.TemporaryDirectory() as directory:
            stats = Path(directory) / "locust_stats.csv"
            stats.write_text("Type,Name,Request Count,Failure Count\n", encoding="utf-8")
            missing = json.loads(json.dumps(sample))
            missing["production"] = None
            _, reason = observe(missing, state, locust_stats=stats)
            self.assertEqual(reason, "production_monitor_unavailable")

            restarted = json.loads(json.dumps(sample))
            restarted["production"]["containers"]["binhu-mysql"]["restart_count"] += 1
            _, reason = observe(restarted, state, locust_stats=stats)
            self.assertEqual(reason, "production_container_unhealthy_or_restarted")

            oom = json.loads(json.dumps(sample))
            oom["production"]["containers"]["binhu-mysql"]["oom_killed"] = True
            _, reason = observe(oom, state, locust_stats=stats)
            self.assertEqual(reason, "production_container_unhealthy_or_restarted")

            new_error = json.loads(json.dumps(sample))
            new_error["production"]["error_count"] = 1
            _, reason = observe(new_error, state, locust_stats=stats)
            self.assertEqual(reason, "production_new_error_threshold_exceeded")

    def test_cumulative_production_errors_do_not_roll_with_sliding_window(self):
        sample = self._safety_sample(production_errors=0)
        policy = preflight(sample, run_id=RUN_ID, waive_memory_gate=True)
        monitor = ProductionStopMonitor(policy["production_baseline"])
        first_zero = json.loads(json.dumps(sample["production"]))
        self.assertEqual(monitor.observe(first_zero), "")
        second_zero = json.loads(json.dumps(sample["production"]))
        self.assertEqual(monitor.observe(second_zero), "")
        new_error = json.loads(json.dumps(sample["production"]))
        new_error["error_count"] = 1
        self.assertEqual(monitor.observe(new_error), "production_new_error_threshold_exceeded")

    def test_production_error_reader_uses_one_fixed_start_and_bounded_logs(self):
        class FakeProcess:
            def __init__(self, content):
                self.stdout = io.BytesIO(content)

            def wait(self, timeout=None):
                return 0

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from deploy.environments.event_pipeline import staging_metrics_probe

        old_error = b"ERROR old bounded test line\n"
        old_and_new = old_error + b"ERROR new bounded test line\n"
        processes = [FakeProcess(old_error), FakeProcess(old_and_new)]
        with patch.object(staging_metrics_probe.subprocess, "Popen", side_effect=processes) as popen:
            first = staging_metrics_probe._production_log_error_count("binhu-backend", 1790000000)
            second = staging_metrics_probe._production_log_error_count("binhu-backend", 1790000000)
        self.assertEqual((first, second), (1, 2))
        for call in popen.call_args_list:
            self.assertEqual(call.args[0][call.args[0].index("--since") + 1], "1790000000")
            self.assertEqual(call.args[0][-1], "binhu-backend")

    def test_staging_oom_restart_and_persistent_connection_failures_stop_run(self):
        sample = self._safety_sample()
        policy = preflight(sample, run_id=RUN_ID, waive_memory_gate=True)
        with tempfile.TemporaryDirectory() as directory:
            stats = Path(directory) / "locust_stats.csv"
            stats.write_text("Type,Name,Request Count,Failure Count\nGET,core.query,100,3\n", encoding="utf-8")
            oom = json.loads(json.dumps(sample))
            oom["containers"]["oom_killed_count"] = 1
            _, reason = observe(oom, json.loads(json.dumps(policy)), locust_stats=stats)
            self.assertEqual(reason, "staging_pipeline_container_unhealthy_or_restarted")

            restarted = json.loads(json.dumps(sample))
            restarted["staging_services"]["binhu-staging-backend-1"]["restart_count"] += 1
            _, reason = observe(restarted, json.loads(json.dumps(policy)), locust_stats=stats)
            self.assertEqual(reason, "staging_application_service_unhealthy_or_restarted")

            state = json.loads(json.dumps(policy))
            state["request_failure_started_at"] = __import__("time").time() - 61
            _, reason = observe(sample, state, locust_stats=stats)
            self.assertEqual(reason, "staging_request_failure_rate_above_2_percent_for_60_seconds")

    def test_staging_fixture_model_is_fictional_credential_free_and_large_enough(self):
        accounts = make_accounts()
        tasks = make_tasks(RUN_ID)
        other_tasks = make_tasks("STG-20260920-10")
        def row_keys(items):
            keys = {}
            for item in items:
                parser = get_parser(item["parser_type"])
                row = {column: "" for column in parser.COLUMNS}
                for column in ("身份证号", "身份证号码", "参考身份证号码"):
                    if column in row:
                        row[column] = item["identity_number"]
                for column in ("电话号码", "手机号码", "联系号码"):
                    if column in row:
                        row[column] = item["phone"]
                if "下发日期" in row:
                    row["下发日期"] = "2026-09-20"
                if "接警编号" in row:
                    row["接警编号"] = item["case_number"]
                keys.setdefault(item["parser_type"], []).append(parser.make_row_key(row))
            return keys

        actual_keys = row_keys(tasks)
        other_keys = row_keys(other_tasks)
        self.assertEqual(len(accounts), 75)
        self.assertEqual(len({item["username"] for item in accounts}), 75)
        self.assertTrue(all(item["username"].endswith("@staging") for item in accounts))
        self.assertEqual(len(tasks), 1440)
        self.assertEqual(len({item["identity_number"] for item in tasks}), 1440)
        self.assertEqual(len({item["case_number"] for item in tasks}), 1440)
        self.assertTrue(all(len(item["identity_number"]) == 18 for item in tasks))
        self.assertTrue({item["identity_number"] for item in tasks}.isdisjoint(
            {item["identity_number"] for item in other_tasks}))
        self.assertTrue({item["case_number"] for item in tasks}.isdisjoint(
            {item["case_number"] for item in other_tasks}))
        for parser_type in actual_keys:
            self.assertEqual(len(set(actual_keys[parser_type])), 240)
            self.assertTrue(set(actual_keys[parser_type]).isdisjoint(set(other_keys[parser_type])))
        suzhou = [item for item in tasks if item["parser_type"] == "苏州涉警"]
        other_suzhou = [item for item in other_tasks if item["parser_type"] == "苏州涉警"]
        self.assertEqual(len({item["case_number"] for item in suzhou}), 240)
        self.assertTrue({item["case_number"] for item in suzhou}.isdisjoint(
            {item["case_number"] for item in other_suzhou}))
        self.assertEqual({item["scenario"] for item in tasks}, {"assigned", "unassigned", "assignable"})
        payload = runtime_index("STG-20260920-02", RUN_ID, [{
            "parser_type": "全链条", "row_key": "synthetic", "scenario": "assigned",
        }])
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("password", serialized)
        self.assertNotIn("secret", serialized)
        self.assertFalse(payload["production_data"])
        self.assertEqual(payload["candidate_run_id"], "STG-20260920-02")
        self.assertEqual(payload["load_run_id"], RUN_ID)

    def test_staging_fixture_requires_distinct_well_formed_run_namespace(self):
        for run_id in ("STG-20260920-1", "other-20260920-01", "STG-20260920-01x"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                make_tasks(run_id)

    def test_staging_seeder_uses_app_source_and_all_database_markers(self):
        source = (Path(__file__).parent / "staging" / "seed.py").read_text(encoding="utf-8")
        self.assertIn("create_local_source_row", source)
        self.assertIn("source_kind=\"local_table\"", source)
        self.assertIn("staging:{run_id}:task:", source)
        self.assertIn('"--candidate-run-id"', source)
        self.assertIn('str(task["case_number"])', source)
        self.assertIn('"platform_side_effects"', source)
        self.assertIn("_environment_identity", source)
        self.assertIn("--password-stdin", source)
        self.assertNotIn("password_hash\":", source)

    def test_staging_seeder_keeps_platform_fixtures_on_platform_pool(self):
        source = (Path(__file__).parent / "staging" / "seed.py").read_text(encoding="utf-8")
        self.assertIn('platform_pool = db_manager.get_pool("platform")', source)
        self.assertIn("async with platform_pool.acquire() as conn:", source)
        self.assertIn('online_pool = db_manager.get_pool("online_data")', source)

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
            candidate_id = "STG-20260920-02"
            stage_rows = [
                {"run_id": RUN_ID, "candidate_run_id": candidate_id,
                 "environment": "staging", "production_data": False,
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
            merged = merge_samples(RUN_ID, staging, production, candidate_id)
            self.assertEqual(merged["candidate_run_id"], candidate_id)
            self.assertTrue(merged["samples"][0]["production"]["healthy"])
            prod_rows[0]["read_only"] = False
            production.write_text("\n".join(json.dumps(x) for x in prod_rows), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                merge_samples(RUN_ID, staging, production)
            stage_rows[0]["candidate_run_id"] = "STG-20260920-03"
            staging.write_text("\n".join(json.dumps(x) for x in stage_rows), encoding="utf-8")
            prod_rows[0]["read_only"] = True
            production.write_text("\n".join(json.dumps(x) for x in prod_rows), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "candidate identity"):
                merge_samples(RUN_ID, staging, production, candidate_id)

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

    def test_evidence_guard_requires_staging_load_identity_without_database_access(self):
        from staging.guard import require_staging_evidence_context

        candidate = env()
        candidate.pop("STAGING_BASE_URL")
        candidate.pop("STAGING_DB_NAME")
        candidate.pop("STAGING_LOAD_TEST_PASSWORD")
        with patch.dict("os.environ", candidate, clear=True):
            context = require_staging_evidence_context(RUN_ID)
            self.assertEqual(context.project, f"binhu-staging-load-{RUN_ID.lower()}")
            self.assertEqual(context.db_name, "evidence-only")
            with patch.dict("os.environ", {"COMPOSE_PROJECT_NAME": "binhu-production"}, clear=False):
                with self.assertRaises(StagingSafetyError):
                    require_staging_evidence_context(RUN_ID)

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

    def test_low20_profile_is_fixed_and_selects_each_business_role(self):
        profile = get_profile("low20")
        self.assertEqual((profile.users, profile.spawn_rate, profile.duration_seconds), (20, 2, 600))
        self.assertFalse(profile.equivalent_to_full_gate)
        selected = select_accounts(make_accounts(), profile)
        self.assertEqual(len(selected), 20)
        self.assertEqual(
            {role: sum(item["role"] == role for item in selected) for role, _ in profile.role_counts},
            dict(profile.role_counts),
        )
        self.assertFalse(profile_payload(profile)["equivalent_to_full_75_user_gate"])
        with self.assertRaisesRegex(ValueError, "unsupported_staging_load_profile"):
            get_profile("20")

    def test_low20_runtime_index_selects_role_balanced_users_from_fictional_fixtures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps(runtime_payload(), ensure_ascii=False), encoding="utf-8")
            runtime = load_runtime_index(str(path), RUN_ID, "low20")
            self.assertEqual(len(runtime.accounts), 20)
            self.assertEqual({item.role for item in runtime.accounts},
                             {"member", "leader", "internal_business", "admin", "super_admin"})

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
        self.assertFalse(result["unverified"])
        self.assertEqual(result["mysql"]["deadlock_delta"], 0)
        self.assertEqual(result["mysql"]["pool_usage_ratio_peak"], .75)
        self.assertEqual(result["kafka"]["lag_final"], 0)
        self.assertEqual(result["flink"]["checkpoint_completed_delta"], 2)
        self.assertEqual(result["derived_queue"]["drain_seconds"], 20)
        self.assertTrue(result["production"]["healthy_all_samples"])

    def test_missing_optional_deadlock_counter_preserves_other_resource_gates(self):
        first = {
            "mysql": {"Threads_connected": 4, "Threads_running": 1,
                      "Innodb_row_lock_current_waits": 0, "Innodb_row_lock_time_max": 25},
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
        second["mysql"]["Threads_connected"] = 6
        second["backend_pool"]["usage_ratio"] = .75
        second["kafka"]["lag"] = 0
        second["flink"]["checkpoint_completed"] = 3
        second["derived_queue"] = {"pending": 0, "drain_seconds": 20}

        summary = summarize_resource_samples([first, second])
        self.assertFalse(summary["unverified"])
        self.assertIsNone(summary["mysql"]["deadlock_delta"])
        self.assertEqual(summary["mysql"]["connections_peak"], 6)
        self.assertEqual(summary["mysql"]["pool_usage_ratio_peak"], .75)

        result = evaluate_report({"resources": summary})
        resource_unverified = [name for name in result["unverified"] if name.startswith("resources.")]
        self.assertEqual(resource_unverified, ["resources.mysql.deadlock_delta"])
        resource_checks = {
            check["name"]: check["status"] for check in result["checks"]
            if check["name"].startswith("resources.")
        }
        self.assertEqual(resource_checks["resources.mysql.deadlock_delta"], "unverified")
        self.assertEqual(resource_checks["resources.mysql.pool_usage_ratio_peak"], "passed")
        self.assertEqual(resource_checks["resources.kafka.lag_final"], "passed")

    def test_model_uses_timed_pollers_long_streams_and_current_write_contract(self):
        source = (Path(__file__).parent / "staging" / "locustfile.py").read_text(encoding="utf-8")
        for marker in (
            "self._spawn_poll(30, self._heartbeat)", "self._spawn_poll(30, self._unread)",
            "self._spawn_poll(60, self._auth_refresh)", "self._spawn_poll(30, self._query_data)",
            "self._spawn_poll(15, self._query_version)", 'name="poll.query_data"',
            "run_sse", "run_query_websocket", "source-rows/{source_id}", "bulk-assign",
            '"device_type": "desktop"', '"device_type": "mobile"',
            '"core.multi_client_pair"', '"poll.multi_client_auth_refresh"',
        ):
            self.assertIn(marker, source)
        self.assertNotIn("@task(6)\n    def polling_heartbeat", source)
        self.assertIn('API_PREFIX = "/staging/api"', source)
        self.assertIn('"layers.poll.auth_refresh.success_rate": ("min", .995)',
                      (Path(__file__).parent / "staging" / "report.py").read_text(encoding="utf-8"))

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

            candidate_id = "STG-20260920-02"
            candidate_manifest = {**manifest, "run_id": candidate_id}
            path.write_text(json.dumps(candidate_manifest), encoding="utf-8")
            self.assertEqual(load_manifest(str(path), RUN_ID, candidate_id)["run_id"], candidate_id)
            with self.assertRaisesRegex(RuntimeError, "运行编号"):
                load_manifest(str(path), RUN_ID, "STG-20260920-03")

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

    def test_verify_cli_binds_candidate_and_load_run_ids_separately(self):
        candidate_id = "STG-20260920-02"
        manifest = {
            "candidate_image_digest": "sha256:" + "a" * 64,
            "commit": "b" * 40, "configuration_sha256": "c" * 64,
            "staging_snapshot_id": "staging-" + "d" * 16,
            "dev_acceptance": {"run_id": "dev-20260919-test"},
            "event_pipeline_image_digests": DIGESTS,
        }
        argv = [
            "staging.control", "verify", "--run-id", RUN_ID,
            "--candidate-run-id", candidate_id, "--manifest", "manifest.json",
            "--load-report", "load.json", "--resource-samples", "resources.json",
            "--rollback-report", "rollback.json", "--manual-report", "manual.json",
        ]
        output = io.StringIO()
        with (
            patch.object(staging_control, "require_staging_evidence_context",
                         return_value=SimpleNamespace(run_id=RUN_ID, project="load", db_name="StagingLoad_test")),
            patch.object(staging_control, "load_manifest", return_value=manifest) as load,
            patch.object(staging_control, "assemble_report", return_value={"passed": True}) as assemble,
            patch.object(sys, "argv", argv),
            __import__("contextlib").redirect_stdout(output),
        ):
            self.assertEqual(staging_control.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["candidate_run_id"], candidate_id)
        self.assertEqual(result["load_run_id"], RUN_ID)
        load.assert_called_once_with("manifest.json", RUN_ID, candidate_id)
        self.assertEqual(assemble.call_args.kwargs["run_id"], RUN_ID)
        self.assertEqual(assemble.call_args.kwargs["candidate_run_id"], candidate_id)

        with (
            patch.object(staging_control, "require_staging_evidence_context",
                         return_value=SimpleNamespace(run_id=RUN_ID, project="load", db_name="StagingLoad_test")),
            patch.object(sys, "argv", [
                "staging.control", "verify", "--run-id", RUN_ID,
                "--candidate-run-id", RUN_ID, "--manifest", "manifest.json",
            ]),
        ):
            with self.assertRaisesRegex(RuntimeError, "distinct"):
                staging_control.main()

    def test_final_report_requires_load_resources_rollback_and_manual_acceptance(self):
        candidate_id = "STG-20260920-02"
        load = {
            "run_id": RUN_ID, "load_run_id": RUN_ID, "candidate_run_id": candidate_id,
            "environment": "staging", "production_data": False,
            "execution_policy": {
                "users": 75,
                "workload_profile": profile_payload(get_profile("full75")),
                "memory_gate_approval_basis": "not_applicable",
                "production_monitor": "passed",
                "memory_gate": {
                    "required_bytes": 4 * 1024**3,
                    "observed_bytes": 4 * 1024**3,
                    "waiver_requested": False,
                    "waived": False,
                    "status": "passed",
                },
            },
            "layers": {
                "core.save": {"p95_ms": 1000},
                "core.multi_client_pair": {"success_rate": 1.0},
                "poll.heartbeat": {"p95_ms": 100, "success_rate": 1.0},
                "poll.unread": {"p95_ms": 100, "success_rate": 1.0},
                "poll.maintenance": {"p95_ms": 100, "success_rate": 1.0},
                "poll.auth_refresh": {"p95_ms": 100, "success_rate": 1.0},
                "poll.multi_client_auth_refresh": {"p95_ms": 100, "success_rate": 1.0},
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
        envelope = {"run_id": RUN_ID, "candidate_run_id": candidate_id,
                    "environment": "staging", "production_data": False,
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
                candidate_run_id=candidate_id,
                load_report_path=root / "load.json",
                resource_samples_path=root / "resources.json",
                rollback_report_path=root / "rollback.json",
                manual_report_path=root / "manual.json",
            )
            self.assertTrue(report["passed"])
            self.assertEqual(report["load_run_id"], RUN_ID)
            self.assertEqual(report["candidate_run_id"], candidate_id)
            load["execution_policy"].update({
                "users": 20,
                "workload_profile": profile_payload(get_profile("low20")),
                "memory_gate_approval_basis": "user-approved low20 profile",
            })
            (root / "load.json").write_text(json.dumps(load), encoding="utf-8")
            low_load = assemble_report(
                run_id=RUN_ID,
                candidate_run_id=candidate_id,
                load_report_path=root / "load.json",
                resource_samples_path=root / "resources.json",
                rollback_report_path=root / "rollback.json",
                manual_report_path=root / "manual.json",
            )
            self.assertTrue(low_load["load_profile_passed"])
            self.assertFalse(low_load["passed"])
            self.assertFalse(low_load["promotion_ready"])
            self.assertTrue(any("not equivalent" in item for item in low_load["acceptance_limitations"]))
            load["execution_policy"].update({
                "users": 75,
                "workload_profile": profile_payload(get_profile("full75")),
                "memory_gate_approval_basis": "not_applicable",
            })
            load["execution_policy"]["memory_gate"].update({
                "observed_bytes": 3 * 1024**3,
                "waiver_requested": True,
                "waived": True,
                "status": "waived",
            })
            load["execution_policy"]["memory_gate_approval_basis"] = "explicit workflow dispatch approval"
            (root / "load.json").write_text(json.dumps(load), encoding="utf-8")
            limited = assemble_report(
                run_id=RUN_ID,
                candidate_run_id=candidate_id,
                load_report_path=root / "load.json",
                resource_samples_path=root / "resources.json",
                rollback_report_path=root / "rollback.json",
                manual_report_path=root / "manual.json",
            )
            self.assertTrue(limited["limited_acceptance"])
            self.assertFalse(limited["passed"])
            self.assertFalse(limited["promotion_ready"])
            self.assertIn("4 GiB", limited["acceptance_limitations"][0])
            load["execution_policy"]["memory_gate"].update({
                "observed_bytes": 4 * 1024**3,
                "waiver_requested": False,
                "waived": False,
                "status": "passed",
            })
            load["execution_policy"]["memory_gate_approval_basis"] = "not_applicable"
            (root / "load.json").write_text(json.dumps(load), encoding="utf-8")
            rollback["data_consistency_verified"] = False
            (root / "rollback.json").write_text(json.dumps(rollback), encoding="utf-8")
            self.assertFalse(assemble_report(
                run_id=RUN_ID,
                candidate_run_id=candidate_id,
                load_report_path=root / "load.json",
                resource_samples_path=root / "resources.json",
                rollback_report_path=root / "rollback.json",
                manual_report_path=root / "manual.json",
            )["passed"])


if __name__ == "__main__":
    unittest.main()
