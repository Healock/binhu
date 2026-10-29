import json
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile
import subprocess
from contextlib import redirect_stdout
from io import StringIO

from deploy.environments.event_pipeline import (
    staging_backend_network, staging_compose, staging_control, staging_deploy,
    staging_metrics_probe, staging_network_cleanup, staging_prepare,
    staging_schema_registry_preload,
)


_GATEWAY_SPEC = importlib.util.spec_from_file_location(
    "staging_gateway", Path(__file__).parents[1] / "environments" / "event_pipeline" /
    "binhu-staging-event-pipeline-gateway.py"
)
staging_gateway = importlib.util.module_from_spec(_GATEWAY_SPEC)
assert _GATEWAY_SPEC.loader is not None
_GATEWAY_SPEC.loader.exec_module(staging_gateway)


RUN_ID = "STG-20260920-01"


def images():
    return {name: "sha256:" + str(index) * 64
            for index, name in enumerate(sorted(staging_compose.IMAGE_KEYS), start=1)}


class StagingGatewayDiagnosticTests(unittest.TestCase):
    def test_prepare_failure_reader_emits_only_fixed_fields_without_writing(self):
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            evidence = state / "evidence" / "STG-20261028-02"
            evidence.mkdir(parents=True)
            source = evidence / "prepare-gateway-0123456789abcdef.json"
            source.write_text(json.dumps({
                "environment": "staging", "run_id": "STG-20261028-02",
                "phase": "prepare", "status": "failed", "failure_stage": "gateway",
                "error_type": "SystemExit", "error_code": "staging_candidate_bundle_hash_mismatch",
                "exit_code": 1, "stdout_sha256": "a" * 64, "stderr_sha256": "b" * 64,
                "stdout_bytes": 27, "stderr_bytes": 41,
                "stdout": "secret stdout", "stderr": "password=hidden", "path": "/private/path",
            }), encoding="utf-8")
            original = source.read_bytes()
            output = StringIO()
            with (
                patch.object(staging_gateway, "STATE", state),
                patch.object(staging_gateway, "CANDIDATES", state / "candidates"),
                redirect_stdout(output),
            ):
                staging_gateway.diagnose_prepare_failure("STG-20261028-02")
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["candidate_present"])
            self.assertEqual(result["candidate_entry"], "missing")
            record = result["failures"][0]
            self.assertEqual(record["failure_stage"], "gateway")
            self.assertEqual(record["exit_code"], 1)
            self.assertEqual(record["evidence_sha256"], hashlib.sha256(original).hexdigest())
            for private in ("secret stdout", "password=hidden", "/private/path"):
                self.assertNotIn(private, output.getvalue())
            self.assertEqual(source.read_bytes(), original)

    def test_prepare_failure_reader_reports_missing_evidence_and_rejects_other_run(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            output = StringIO()
            with (
                patch.object(staging_gateway, "STATE", state),
                patch.object(staging_gateway, "CANDIDATES", state / "candidates"),
                redirect_stdout(output),
            ):
                staging_gateway.diagnose_prepare_failure("STG-20261028-02")
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "evidence_missing")
            self.assertEqual(result["failure_count"], 0)
            self.assertEqual(result["candidate_entry"], "missing")

        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as raised:
            staging_gateway.diagnose_prepare_failure(RUN_ID)
        self.assertIn("prepare diagnostic target is not approved", str(raised.exception))

    def test_prepare_failure_reader_accepts_existing_child_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            evidence = state / "evidence" / "STG-20261028-02"
            evidence.mkdir(parents=True)
            source = evidence / "prepare-0123456789abcdef.json"
            source.write_text(json.dumps({
                "environment": "staging", "run_id": "STG-20261028-02",
                "phase": "prepare", "status": "failed", "error_type": "ValueError",
                "error_code": "staging_candidate_identity_mismatch",
                "child_return_code": 23, "stdout_sha256": "c" * 64,
                "stderr_sha256": "d" * 64, "stdout_bytes": 10, "stderr_bytes": 20,
                "stderr": "private response must not escape",
            }), encoding="utf-8")
            output = StringIO()
            with (
                patch.object(staging_gateway, "STATE", state),
                patch.object(staging_gateway, "CANDIDATES", state / "candidates"),
                redirect_stdout(output),
            ):
                staging_gateway.diagnose_prepare_failure("STG-20261028-02")
            result = json.loads(output.getvalue())
            record = result["failures"][0]
            self.assertEqual(record["failure_stage"], "child")
            self.assertEqual(record["exit_code"], 23)
            self.assertEqual(record["stderr_sha256"], "d" * 64)
            self.assertNotIn("private response", output.getvalue())

    def test_prepare_gateway_rejection_writes_only_fixed_failure_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            candidates = state / "candidates"
            with (
                patch.object(staging_gateway, "STATE", state),
                patch.object(staging_gateway, "CANDIDATES", candidates),
                patch.object(staging_gateway, "read_bundle", side_effect=SystemExit(
                    "Staging event-pipeline gateway refused: candidate bundle hash mismatch"
                )),
                self.assertRaises(SystemExit),
            ):
                staging_gateway.prepare(
                    "STG-20261028-02", "a" * 40, "32", "b" * 64,
                )
            self.assertFalse((candidates / "STG-20261028-02").exists())
            failure = next((state / "evidence" / "STG-20261028-02").glob("prepare-gateway-*.json"))
            payload = json.loads(failure.read_text(encoding="utf-8"))
            self.assertEqual(payload["failure_stage"], "gateway")
            self.assertEqual(payload["error_code"], "staging_candidate_bundle_hash_mismatch")
            self.assertEqual(payload["exit_code"], 1)
            self.assertEqual(payload["stdout_bytes"], 0)
            self.assertEqual(payload["stderr_bytes"], 0)

    def test_measure_failure_reader_emits_only_fixed_fields_without_writing(self):
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            evidence = state / "evidence" / "STG-20261028-01"
            evidence.mkdir(parents=True)
            source = evidence / "measure-0123456789abcdef.json"
            source.write_text(json.dumps({
                "environment": "staging", "run_id": "STG-20261028-01",
                "phase": "measure", "status": "failed", "error_type": "ValueError",
                "error_code": "staging_insufficient_memory",
                "diagnostic_code": "staging_insufficient_memory",
                "child_return_code": 1, "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64, "stdout_bytes": 23, "stderr_bytes": 41,
                "stdout": "secret output", "stderr": "SELECT sensitive_value",
                "password": "must-not-leak", "path": "/private/server/path",
            }), encoding="utf-8")
            original = source.read_bytes()
            output = StringIO()
            with (
                patch.object(staging_gateway, "STATE", state),
                redirect_stdout(output),
            ):
                staging_gateway.diagnose_measure_failure("STG-20261028-01")
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "failed")
            record = result["failures"][0]
            self.assertEqual(record["error_code"], "staging_insufficient_memory")
            self.assertEqual(record["child_return_code"], 1)
            self.assertEqual(record["evidence_sha256"], hashlib.sha256(original).hexdigest())
            for private in ("secret output", "sensitive_value", "must-not-leak", "/private/"):
                self.assertNotIn(private, output.getvalue())
            self.assertEqual(source.read_bytes(), original)

    def test_measure_failure_reader_rejects_unapproved_run_id(self):
        output = StringIO()
        with (
            redirect_stdout(output), self.assertRaises(SystemExit) as raised,
        ):
            staging_gateway.diagnose_measure_failure(RUN_ID)
        self.assertIn("measure diagnostic target is not approved", str(raised.exception))
        self.assertEqual(output.getvalue(), "")

    def test_measure_failure_reader_rejects_mismatched_evidence_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            evidence = state / "evidence" / "STG-20261028-01"
            evidence.mkdir(parents=True)
            (evidence / "measure-0123456789abcdef.json").write_text(json.dumps({
                "environment": "production", "run_id": "STG-20261028-01",
                "phase": "measure", "status": "failed", "error_type": "ValueError",
                "error_code": "staging_measure_failed",
            }), encoding="utf-8")
            with patch.object(staging_gateway, "STATE", state):
                with self.assertRaisesRegex(SystemExit, "measure failure evidence identity is invalid"):
                    staging_gateway.diagnose_measure_failure("STG-20261028-01")

    def test_child_payload_accepts_prefixed_json_without_forwarding_text(self):
        payloads = staging_gateway._child_payloads(
            'warning {"environment":"staging","run_id":"STG-20260920-01",'
            '"status":"failed","phase":"prepare","error_type":"ValueError",'
            '"error_code":"staging_image_identity_mismatch"}', "secret output"
        )
        self.assertEqual(payloads[0]["error_code"], "staging_image_identity_mismatch")
        self.assertNotIn("secret output", json.dumps(payloads))

    def test_child_failure_evidence_contains_only_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            payload = {"error_type": "ValueError", "error_code": "staging_prepare_failed"}
            with patch.object(staging_gateway, "STATE", state):
                staging_gateway._record_child_failure(
                    "STG-20260920-01", "prepare", payload, "sensitive stdout", "sensitive stderr"
                )
            evidence = list((state / "evidence" / "STG-20260920-01").glob("*.json"))
            self.assertEqual(len(evidence), 1)
            text = evidence[0].read_text(encoding="utf-8")
            self.assertNotIn("sensitive", text)
            self.assertIn("stdout_sha256", text)
            self.assertIn("stderr_sha256", text)

    def test_measure_child_failure_records_safe_code_and_return_code_only(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            safe_child = json.dumps({
                "environment": "staging", "run_id": RUN_ID, "status": "failed",
                "phase": "measure", "error_type": "ValueError",
                "error_code": "staging_measure_contract_rejected",
            })
            error = subprocess.CalledProcessError(
                1, ["python", "-m", "event_pipeline.staging_control"],
                output=safe_child, stderr="SELECT sensitive_value; secret=hidden",
            )
            output = StringIO()
            with patch.object(staging_gateway, "STATE", state), \
                    patch.object(staging_gateway, "environment", return_value={}), \
                    patch.object(staging_gateway.subprocess, "run", side_effect=error), \
                    redirect_stdout(output), self.assertRaises(subprocess.CalledProcessError):
                staging_gateway.run_module(RUN_ID, "measure")
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["phase"], "measure")
            self.assertEqual(payload["error_code"], "staging_measure_contract_rejected")
            self.assertEqual(payload["child_return_code"], 1)
            evidence = next((state / "evidence" / RUN_ID).glob("*.json"))
            recorded = json.loads(evidence.read_text(encoding="utf-8"))
            text = evidence.read_text(encoding="utf-8")
            self.assertEqual(recorded["child_return_code"], 1)
            self.assertEqual(recorded["diagnostic_code"], "staging_measure_contract_rejected")
            self.assertNotIn("SELECT", text)
            self.assertNotIn("sensitive_value", text)
            self.assertNotIn("hidden", text)

    def test_module_and_path_failures_are_classified_without_raw_output(self):
        cases = (
            ("ModuleNotFoundError: No module named '/private/module'", "staging_module_import_failed"),
            ("FileNotFoundError: /private/path/with-sensitive-name", "staging_required_file_missing"),
        )
        for stderr, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as directory:
                    error = subprocess.CalledProcessError(1, ["python"], stderr=stderr)
                    output = StringIO()
                    with patch.object(staging_gateway, "STATE", Path(directory)), \
                            patch.object(staging_gateway, "environment", return_value={}), \
                            patch.object(staging_gateway.subprocess, "run", side_effect=error), \
                            redirect_stdout(output), self.assertRaises(subprocess.CalledProcessError):
                        staging_gateway.run_module(RUN_ID, "measure")
                    diagnostic = json.loads(output.getvalue())
                    self.assertEqual(diagnostic["error_code"], expected_code)
                    self.assertEqual(diagnostic["phase"], "measure")
                    evidence = next((Path(directory) / "evidence" / RUN_ID).glob("*.json"))
                    text = evidence.read_text(encoding="utf-8")
                    self.assertNotIn("/private/", text)

    def test_measure_failure_emits_fixed_sanitized_child_diagnostic(self):
        output = StringIO()
        with patch("sys.argv", ["staging_control", "measure", "--run-id", RUN_ID]), \
                patch.object(staging_control, "measure", side_effect=ValueError("raw SQL and secret")), \
                redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            staging_control.main()
        self.assertEqual(raised.exception.code, 1)
        diagnostic = json.loads(output.getvalue())
        self.assertEqual(diagnostic, {
            "environment": "staging", "run_id": RUN_ID, "status": "failed",
            "phase": "measure", "error_type": "ValueError",
            "error_code": "staging_measure_contract_rejected",
        })
        self.assertNotIn("raw SQL", output.getvalue())

    def test_child_launch_path_failure_is_recorded_without_path_text(self):
        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            with patch.object(staging_gateway, "STATE", Path(directory)), \
                    patch.object(staging_gateway, "environment", return_value={}), \
                    patch.object(staging_gateway.subprocess, "run",
                                 side_effect=FileNotFoundError("/private/python-with-secret")), \
                    redirect_stdout(output), self.assertRaises(FileNotFoundError):
                staging_gateway.run_module(RUN_ID, "measure")
            diagnostic = json.loads(output.getvalue())
            self.assertEqual(diagnostic["error_type"], "FileNotFoundError")
            self.assertEqual(diagnostic["error_code"], "staging_child_launch_failed")
            evidence = next((Path(directory) / "evidence" / RUN_ID).glob("*.json"))
            self.assertNotIn("/private/", evidence.read_text(encoding="utf-8"))

    def test_failed_retirement_accepts_terminal_flink_jobs_as_safe_summaries(self):
        payload = {
            "jobs": [
                {"jid": "canceled-jid", "name": "SELECT sensitive-looking text", "state": "CANCELED"},
                {"jid": "finished-jid", "name": "another terminal job", "state": "FINISHED"},
                {"jid": "target-jid", "name": "STG-20261027-04", "state": "FAILED"},
            ]
        }
        completed = subprocess.CompletedProcess(
            ["docker", "exec"], 0, json.dumps(payload), ""
        )
        with patch.object(staging_gateway.subprocess, "run", return_value=completed):
            jobs = staging_gateway._retirement_jobs("jobmanager", "STG-20261027-04")
        self.assertEqual(
            jobs,
            [
                {"jid": "canceled-jid", "name": "terminal-job", "state": "CANCELED"},
                {"jid": "finished-jid", "name": "terminal-job", "state": "FINISHED"},
                {"jid": "target-jid", "name": "STG-20261027-04", "state": "FAILED"},
            ],
        )

    def test_failed_retirement_rejects_running_foreign_flink_job(self):
        payload = {"jobs": [{"jid": "foreign-jid", "name": "foreign-run", "state": "RUNNING"}]}
        completed = subprocess.CompletedProcess(
            ["docker", "exec"], 0, json.dumps(payload), ""
        )
        with patch.object(staging_gateway.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "staging_retirement_foreign_flink_job"):
                staging_gateway._retirement_jobs("jobmanager", "STG-20261027-04")

    def test_stream_clients_can_be_loaded_as_locust_top_level_module(self):
        staging_dir = Path(__file__).parents[2] / "load-tests" / "staging"
        spec = importlib.util.spec_from_file_location("stream_clients", staging_dir / "stream_clients.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        import sys
        sys.modules[spec.name] = module
        with patch("sys.path", [str(staging_dir), *__import__("sys").path]):
            spec.loader.exec_module(module)
        self.assertTrue(callable(module.retry_delay))


class StagingEventPipelineComposeTests(unittest.TestCase):
    def test_network_address_pool_exhaustion_signature_is_classified(self):
        result = subprocess.CompletedProcess(
            ["docker", "network", "create"], 1, "",
            "Error response from daemon: could not find an available, non-overlapping IPv4 address pool among the defaults to assign to the network",
        )
        with patch("deploy.environments.event_pipeline.staging_control.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ValueError, "staging_network_address_pool_exhausted"):
                staging_control._run(["docker", "network", "create"])


class StagingArchitectureSwitchTests(unittest.TestCase):
    def _candidate(self, directory: str, *, started: bool = True) -> tuple[Path, dict]:
        root = Path(directory) / "candidate"
        (root / "evidence").mkdir(parents=True)
        manifest = {
            "environment": "staging",
            "run_id": RUN_ID,
            "project": "binhu-staging-event-pipeline-stg-20260920-01",
            "started": started,
            "acceptance": "pending",
        }
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root, manifest

    def test_switch_requires_an_applied_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root, manifest = self._candidate(directory, started=False)
            with patch.object(staging_control, "_load", return_value=(root, manifest, {})), \
                    patch.object(staging_control, "_compose") as compose:
                with self.assertRaisesRegex(ValueError, "must be applied"):
                    staging_control.switch(RUN_ID)
            compose.assert_not_called()

    def test_switch_stops_python_worker_and_records_flink_as_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root, manifest = self._candidate(directory)
            ps_calls = 0

            def compose(_root, *args, **_kwargs):
                nonlocal ps_calls
                if args and args[0] == "ps":
                    ps_calls += 1
                    state = "running" if ps_calls == 1 else "exited (0)"
                    return subprocess.CompletedProcess(args, 0, json.dumps({
                        "Service": "python-metadata-worker", "State": state,
                    }))
                return subprocess.CompletedProcess(args, 0, "")

            with patch.object(staging_control, "_load", return_value=(root, manifest, {})), \
                    patch.object(staging_control.flink_submission, "FlinkRest"), \
                    patch.object(staging_control.flink_submission, "partition_active_jobs",
                                 return_value=([{"jid": "a" * 32}], [])), \
                    patch.object(staging_control.flink_submission, "wait_for_runtime",
                                 return_value={"run_id": RUN_ID}), \
                    patch.object(staging_control, "_compose", side_effect=compose):
                result = staging_control.switch(RUN_ID)

            self.assertEqual(result["active_engine"], "flink")
            self.assertTrue(result["python_worker_stopped"])
            self.assertEqual(result["business_writes"], "mysql_outbox")
            updated = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(updated["architecture"]["active"])
            self.assertEqual(updated["architecture"]["projection_engine"], "flink")
            self.assertEqual(updated["architecture"]["application_read_source"], "mysql")
            evidence = list((root / "evidence").glob("switch-*/switch.json"))
            self.assertEqual(len(evidence), 1)

    def test_switch_failure_keeps_fixed_diagnostic_and_does_not_expose_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root, manifest = self._candidate(directory)
            class Client:
                def overview(self):
                    return []

            client = Client()
            with patch.object(staging_control, "_load", return_value=(root, manifest, {})), \
                    patch.object(staging_control.flink_submission, "FlinkRest", return_value=client), \
                    patch.object(staging_control.flink_submission, "partition_active_jobs",
                                 return_value=([{"jid": "b" * 32}], [])), \
                    patch.object(staging_control.flink_submission, "wait_for_runtime",
                                 side_effect=ValueError("private response body")), \
                    patch.object(staging_control, "_compose") as compose:
                with self.assertRaisesRegex(ValueError, "private response body"):
                    staging_control.switch(RUN_ID)
            compose.assert_not_called()
            evidence = list((root / "evidence").glob("switch-*/switch-failure.json"))
            self.assertEqual(len(evidence), 1)
            text = evidence[0].read_text(encoding="utf-8")
            self.assertIn('"error_code": "staging_architecture_switch_failed"', text)
            self.assertNotIn("private response body", text)

    def test_complete_model_is_isolated_and_bounded(self):
        spec = staging_compose.compose(images(), RUN_ID)
        self.assertEqual(spec["name"], "binhu-staging-event-pipeline-stg-20260920-01")
        self.assertTrue(spec["networks"]["internal"]["internal"])
        self.assertEqual(spec["networks"]["backend"], {
            "external": True, "name": "binhu-staging-pipeline-backend",
        })
        self.assertEqual(set(spec["services"]), {
            "staging-kafka-1", "staging-kafka-2", "staging-kafka-3", "schema-registry",
            "staging-derived-mysql", "staging-derived-redis", "relay", "bridge",
            "business-bridge", "backend-outbox-relay", "python-metadata-worker",
            "jobmanager", "taskmanager",
        })
        for name, service in spec["services"].items():
            self.assertEqual(service["labels"]["binhu.environment"], "staging", name)
            self.assertEqual(service["labels"]["binhu.run_id"], RUN_ID, name)
            self.assertEqual(service["labels"]["binhu.production_data"], "false", name)
            self.assertNotIn("ports", service, name)
            self.assertNotIn("privileged", service, name)
            self.assertEqual(service["logging"]["options"], {"max-size": "5m", "max-file": "2"})
            self.assertGreater(service["pids_limit"], 0)
            self.assertIn("mem_limit", service)
            self.assertIn("cpus", service)

    def test_kafka_storage_topic_and_networks_are_run_scoped(self):
        spec = staging_compose.compose(images(), RUN_ID)
        for index, name in enumerate(staging_compose.BROKERS, start=1):
            service = spec["services"][name]
            self.assertEqual(service["environment"]["KAFKA_NODE_ID"], str(index))
            self.assertEqual(service["environment"]["KAFKA_HEAP_OPTS"], "-Xms256m -Xmx512m")
            self.assertEqual(service["mem_limit"], "1g")
            self.assertEqual(service["memswap_limit"], "1280m")
            self.assertEqual(service["healthcheck"]["timeout"], "15s")
            self.assertEqual(service["healthcheck"]["start_period"], "120s")
            self.assertEqual(service["healthcheck"]["retries"], 30)
            self.assertEqual(service["environment"]["KAFKA_AUTO_CREATE_TOPICS_ENABLE"], "false")
            self.assertIn(f"{name}-data:/var/lib/kafka/data", service["volumes"])
            self.assertIn(f"{name}-secrets-tmpfs:/etc/kafka/secrets", service["volumes"])
            self.assertIn(f"{name}-config-tmpfs:/mnt/shared/config", service["volumes"])
            for suffix in ("secrets-tmpfs", "config-tmpfs"):
                volume = spec["volumes"][f"{name}-{suffix}"]
                self.assertEqual(volume["driver_opts"]["type"], "tmpfs")

    def test_flink_mounts_fixed_staging_connector_dependencies(self):
        spec = staging_compose.compose(images(), RUN_ID)
        volumes = spec["services"]["jobmanager"]["volumes"]
        for name in staging_compose.DEPENDENCIES:
            self.assertIn(
                f"{staging_compose.DEPENDENCY_ROOT}/{name}:/opt/flink/lib/{name}:ro",
                volumes,
            )
        self.assertNotIn("development", " ".join(volumes).lower())
        self.assertNotIn("dev-pipeline", " ".join(volumes).lower())
        self.assertEqual(
            spec["services"]["schema-registry"]["environment"]["REGISTRY_KAFKASQL_TOPIC"],
            "staging.registry.storage.v1",
        )
        self.assertNotIn("dev.task.events", str(spec))
        self.assertNotIn("binhu-development", str(spec))
        self.assertNotIn("binhu-production", str(spec))

    def test_kafka_quorum_wait_returns_only_aggregate_status(self):
        calls = []

        def checked(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "broker output", "")

        with patch.object(staging_control, "_run", side_effect=checked):
            result = staging_control._wait_for_kafka_quorum("binhu-staging-event-pipeline-stg-20260920-01")
        self.assertTrue(result["ready"])
        self.assertEqual(set(result["brokers"]), set(staging_compose.BROKERS))
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("kafka-broker-api-versions.sh" in " ".join(command) for command in calls))
        self.assertNotIn("broker output", json.dumps(result))

    def test_kafka_quorum_wait_classifies_timeout(self):
        with patch.object(staging_control, "_run", side_effect=ValueError("staging_resource_exhausted")), \
                patch.object(staging_control.time, "monotonic", side_effect=[0, 181]), \
                patch.object(staging_control.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "staging_kafka_quorum_timeout"):
                staging_control._wait_for_kafka_quorum(
                    "binhu-staging-event-pipeline-stg-20260920-01", timeout=180,
                )

    def test_kafka_quorum_wait_redacts_probe_timeout(self):
        with patch.object(staging_control, "_run", side_effect=subprocess.TimeoutExpired("docker", 20)), \
                patch.object(staging_control.time, "monotonic", side_effect=[0, 181]), \
                patch.object(staging_control.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "staging_kafka_quorum_timeout"):
                staging_control._wait_for_kafka_quorum(
                    "binhu-staging-event-pipeline-stg-20260920-01", timeout=180,
                )

    def test_service_health_wait_returns_aggregate_status(self):
        with patch.object(staging_control, "_docker_json", return_value=[{
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
        }]):
            result = staging_control._wait_for_service_healthy(
                "binhu-staging-event-pipeline-stg-20260920-01", "staging-derived-mysql",
            )
        self.assertEqual(result, {
            "service": "staging-derived-mysql", "status": "healthy", "attempts": 1,
        })

    def test_service_health_wait_classifies_timeout(self):
        with patch.object(staging_control, "_docker_json", return_value=[{
            "State": {"Status": "running", "Health": {"Status": "starting"}},
        }]), patch.object(staging_control.time, "monotonic", side_effect=[0, 241]), \
                patch.object(staging_control.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "staging_service_health_timeout"):
                staging_control._wait_for_service_healthy(
                    "binhu-staging-event-pipeline-stg-20260920-01", "staging-derived-mysql",
                    timeout=240,
                )

    def test_missing_consumer_group_is_allowed_before_first_event(self):
        with patch.object(staging_control, "_run", side_effect=ValueError("staging_consumer_group_not_created")):
            self.assertEqual(
                staging_control._consumer_group(
                    "binhu-staging-event-pipeline-stg-20260920-01",
                    "STG-20260920-01-flink",
                ),
                ["STG-20260920-01-flink"],
            )

    def test_empty_consumer_group_report_is_allowed_before_first_event(self):
        with patch.object(
                staging_control, "_run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            self.assertEqual(
                staging_control._consumer_group(
                    "binhu-staging-event-pipeline-stg-20260920-01",
                    "STG-20260920-01-flink",
                ),
                ["STG-20260920-01-flink"],
            )

    def test_consumer_group_headers_without_assignment_are_allowed(self):
        output = (
            "Consumer group 'STG-20260920-01-flink' has no active members.\n"
            "GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG\n"
        )
        with patch.object(
                staging_control, "_run",
                return_value=subprocess.CompletedProcess([], 0, output, ""),
        ):
            self.assertEqual(
                staging_control._consumer_group(
                    "binhu-staging-event-pipeline-stg-20260920-01",
                    "STG-20260920-01-flink",
                ),
                ["STG-20260920-01-flink"],
            )

    def test_consumer_group_report_for_other_topic_is_rejected(self):
        with patch.object(
                staging_control, "_run",
                return_value=subprocess.CompletedProcess(
                    [], 0, "GROUP TOPIC\nSTG-20260920-01-flink other.topic 0", "",
                ),
        ), self.assertRaisesRegex(ValueError, "no Staging topic assignment"):
            staging_control._consumer_group(
                "binhu-staging-event-pipeline-stg-20260920-01",
                "STG-20260920-01-flink",
            )

    def test_checkpoint_volume_identity_is_required_before_owner_change(self):
        from types import SimpleNamespace
        import stat
        volume = {
            "Name": "binhu-staging-event-pipeline-stg-20260920-01_flink-checkpoints",
            "Labels": {"binhu.environment": "staging", "binhu.run_id": "STG-20260920-01"},
            "Mountpoint": "/data/docker/volumes/binhu-staging-event-pipeline-stg-20260920-01_flink-checkpoints/_data",
        }
        with patch.object(staging_control, "_docker_json", return_value=[volume]), \
                patch.object(Path, "is_symlink", return_value=False), \
                patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR)), \
                patch.object(staging_control.os, "chown", create=True) as chown:
            result = staging_control._prepare_checkpoint_volume(
                Path("/fixed/candidate"), "binhu-staging-event-pipeline-stg-20260920-01",
                "STG-20260920-01",
            )
        self.assertEqual(result["owner_uid"], 9999)
        self.assertEqual(result["owner_gid"], 9999)
        chown.assert_called_once()

    def test_backend_relay_is_confined_to_staging_backend_network(self):
        spec = staging_compose.compose(images(), RUN_ID)
        self.assertEqual(spec["services"]["backend-outbox-relay"]["networks"], ["backend"])
        self.assertEqual(set(spec["services"]["business-bridge"]["networks"]), {"internal", "backend"})
        for name, service in spec["services"].items():
            if name not in {"backend-outbox-relay", "business-bridge"}:
                self.assertNotIn("backend", service.get("networks", []), name)

    def test_flink_uses_independent_checkpoint_volume_and_fixed_limits(self):
        spec = staging_compose.compose(images(), RUN_ID)
        for name in ("jobmanager", "taskmanager"):
            service = spec["services"][name]
            self.assertEqual(service["pids_limit"], 256)
            self.assertIn("flink-checkpoints:/opt/flink/checkpoints", service["volumes"])
            self.assertEqual(service["environment"]["APP_ENVIRONMENT"], "staging")
            self.assertEqual(service["environment"]["STAGING_RUN_ID"], RUN_ID)

    def test_identity_and_images_fail_closed(self):
        with self.assertRaises(ValueError):
            staging_compose.compose(images(), "dev-20260920-x")
        with self.assertRaises(ValueError):
            staging_compose.compose({**images(), "kafka": "apache/kafka:latest"}, RUN_ID)
        missing = images()
        missing.pop("schema_registry")
        with self.assertRaises(ValueError):
            staging_compose.compose(missing, RUN_ID)

    def test_model_hash_is_deterministic_and_run_specific(self):
        self.assertEqual(
            staging_compose.model_sha256(images(), RUN_ID),
            staging_compose.model_sha256(images(), RUN_ID),
        )
        self.assertNotEqual(
            staging_compose.model_sha256(images(), RUN_ID),
            staging_compose.model_sha256(images(), "STG-20260920-02"),
        )


class StagingEventPipelinePrepareTests(unittest.TestCase):
    def test_prepare_writes_private_identity_bound_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "staging-event-pipeline"
            source = Path(directory) / "source"
            source.mkdir()
            (source / "__init__.py").write_text("", encoding="utf-8")
            (source / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
            jar = Path(directory) / "pipeline-job.jar"
            jar.write_bytes(b"synthetic-jar")
            env = {
                "STAGING_PIPELINE_MYSQL_PASSWORD": "1" * 48,
                "STAGING_PIPELINE_MYSQL_ROOT_PASSWORD": "2" * 48,
                "STAGING_PIPELINE_REDIS_PASSWORD": "3" * 48,
                "STAGING_BACKEND_MYSQL_PASSWORD": "4" * 48,
                "STAGING_BACKEND_REDIS_URL": "redis://:fixture@environment-redis:6379/0",
            }
            with patch.object(staging_prepare, "BASE", base), patch.object(staging_control, "BASE", base):
                manifest = staging_prepare.prepare(
                    RUN_ID, images(), source=source, pipeline_jar=jar,
                    snapshot_id="staging-" + "d" * 16,
                    environ=env, verify_images=False,
                )
                root = base / RUN_ID
                self.assertEqual(manifest["project"], staging_compose.project_for(RUN_ID))
                self.assertEqual(json.loads((root / "compose.json").read_text()), staging_compose.compose(images(), RUN_ID))
                self.assertIn(f"STAGING_RUN_ID={RUN_ID}", (root / "runtime.env").read_text())
                self.assertIn("APP_ENVIRONMENT=staging", (root / "backend-relay.env").read_text())
                self.assertIn("environment = 'staging'", (root / "pipeline.sql").read_text())
                self.assertIn("staging.task.events.v1", (root / "pipeline.sql").read_text())
                self.assertIn(
                    "BACKEND_MYSQL_DATABASE=Staging_s" + "d" * 16 + "_OnlineData",
                    (root / "backend-relay.env").read_text(),
                )
                self.assertEqual(manifest["staging_snapshot_id"], "staging-" + "d" * 16)
                self.assertEqual((root / "code" / "runtime.py").read_text(), "VALUE = 1\n")
                loaded_root, loaded, _ = staging_control._load(RUN_ID)
                self.assertEqual(loaded_root, root)
                self.assertEqual(loaded["run_id"], RUN_ID)

    def test_prepare_rejects_cross_environment_backend_targets(self):
        with self.assertRaises(ValueError):
            staging_prepare.backend_environment(
                RUN_ID, password="4" * 48,
                redis_url="redis://:fixture@binhu-development-redis:6379/0",
                snapshot_id="staging-" + "d" * 16,
            )

    def test_control_has_no_destructive_volume_removal(self):
        source = Path(staging_control.__file__).read_text(encoding="utf-8")
        self.assertNotIn("down\", \"-v", source)
        self.assertNotIn("docker volume rm", source)
        self.assertIn("event_pipeline.delivery_schema_migrate", source)
        self.assertIn("for attempt in range(6)", source)
        with self.assertRaises(ValueError):
            staging_control._validate_no_cross_environment({"networks": {"x": "binhu-production_internal"}})

    def test_compose_json_parser_accepts_array_and_json_lines(self):
        self.assertEqual(staging_control._json_array_or_lines('[{"Service":"worker"}]')[0]["Service"], "worker")
        self.assertEqual(
            len(staging_control._json_array_or_lines('{"Service":"a"}\n{"Service":"b"}\n')), 2,
        )

    def test_measure_binds_the_active_staging_snapshot_database(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "backend.env"
            snapshot_id = "staging-" + "d" * 16
            env_path.write_text(
                "APP_ENVIRONMENT=staging\nMYSQL_HOST=environment-mysql\n"
                f"MYSQL_ONLINE_DATA_DB=Staging_s{'d' * 16}_OnlineData\n",
                encoding="utf-8",
            )
            with patch.object(staging_control, "Path", lambda value: env_path):
                self.assertEqual(
                    staging_control._active_staging_snapshot({"staging_snapshot_id": snapshot_id}),
                    snapshot_id,
                )

    def test_metrics_probe_exposes_fixed_aggregate_production_monitor(self):
        source = Path(staging_metrics_probe.__file__).read_text(encoding="utf-8")
        for metric in (
            "Innodb_deadlocks", "Innodb_row_lock_current_waits", "keyspace_hits",
            "checkpoint_completed", "_kafka_event_delivery", "restart_count",
            "pool_count", "/api/admin/ops/performance", "production_since_unix",
            "production_bootstrap_identity_mismatch", "production_log_sample_limit_exceeded",
        ):
            self.assertIn(metric, source)
        self.assertEqual(staging_metrics_probe.PRODUCTION_CONTAINER_NAMES,
                         ("binhu-backend", "binhu-mysql", "binhu-ops-agent"))
        self.assertEqual(staging_metrics_probe.PRODUCTION_BOOTSTRAP_URL,
                         "http://127.0.0.1:37125/api/app/bootstrap")
        self.assertEqual(staging_metrics_probe.PRODUCTION_HEALTH_URL,
                         "http://127.0.0.1:37125/api/health")
        production_source = source[source.index("def _production_sample"):source.index("def _staging_service_states")]
        self.assertNotIn("print(", production_source)

    def test_metrics_probe_validates_authorized_pause_evidence_and_eight_database_hashes(self):
        import hashlib

        pause_id = "PROD-PAUSE-20261029-02"
        databases = list(staging_metrics_probe.PRODUCTION_PAUSE_DATABASES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence" / "pause-production" / pause_id
            backup = root / "backup" / f"maintenance-pause-production-{pause_id}"
            evidence.mkdir(parents=True)
            backup.mkdir(parents=True)
            entries = []
            for database in databases:
                path = backup / f"{database}.sql.gz"
                path.write_bytes((database + " synthetic backup").encode())
                entries.append({
                    "database": database, "filename": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })
            manifest = {
                "schema": 1, "operation": "pause-production", "run_id": pause_id,
                "database_scope": databases, "databases": entries,
            }
            manifest_path = backup / "backup-manifest.json"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            pause_path = evidence / "pause.json"
            pause_path.write_text(json.dumps({
                "schema": 1, "operation": "pause-production", "run_id": pause_id,
                "accepted": True, "stopped_services": ["backend", "mysql"],
                "protected_container_states_unchanged": True,
                "backup_manifest_sha256": manifest_sha,
            }), encoding="utf-8")
            with patch.object(staging_metrics_probe, "PRODUCTION_PAUSE_EVIDENCE_ROOT", root / "evidence"), \
                 patch.object(staging_metrics_probe, "PRODUCTION_PAUSE_BACKUP_ROOT", root / "backup"):
                result = staging_metrics_probe._read_pause_evidence(pause_id)
            self.assertTrue(result["pause_evidence_valid"])
            self.assertEqual(result["backup_database_count"], 8)
            self.assertEqual(result["backup_manifest_sha256"], manifest_sha)

    def test_backend_pool_probe_passes_private_password_only_over_stdin(self):
        payload = {"pool_count": 8, "usage_ratio": .5, "used": 20, "max_size": 40}
        with patch.dict("os.environ", {"STAGING_LOAD_TEST_PASSWORD": "x" * 32}, clear=False), \
                patch.object(staging_metrics_probe, "_run", return_value=json.dumps(payload)) as run:
            self.assertEqual(
                staging_metrics_probe._backend_pool("binhu-staging-backend-1", RUN_ID), payload,
            )
        command = run.call_args.args[0]
        self.assertNotIn("x" * 32, " ".join(command))
        self.assertIn("staging-load-admin-01@staging", " ".join(command))
        self.assertNotIn("staging-load-super_admin-01@staging", " ".join(command))
        self.assertEqual(run.call_args.kwargs["input_text"], "x" * 32 + "\n")

    def test_mysql_probe_accepts_server_without_optional_deadlock_counter(self):
        output = "\n".join((
            "Innodb_row_lock_current_waits\t0",
            "Innodb_row_lock_time_max\t12",
            "Threads_connected\t8",
            "Threads_running\t2",
        ))
        with patch.object(staging_metrics_probe, "_run", return_value=output):
            result = staging_metrics_probe._mysql_status("derived-mysql")
        self.assertIsNone(result["Innodb_deadlocks"])
        self.assertEqual(result["Threads_connected"], 8)

    def test_mysql_probe_preserves_measured_zero_deadlock_counter(self):
        output = "\n".join((
            "Innodb_deadlocks\t0",
            "Innodb_row_lock_current_waits\t0",
            "Innodb_row_lock_time_max\t12",
            "Threads_connected\t8",
            "Threads_running\t2",
        ))
        with patch.object(staging_metrics_probe, "_run", return_value=output):
            result = staging_metrics_probe._mysql_status("derived-mysql")
        self.assertEqual(result["Innodb_deadlocks"], 0)

    def test_redis_probe_passes_runtime_password_over_stdin(self):
        info = "\n".join((
            "used_memory:10", "maxmemory:100", "evicted_keys:0",
            "keyspace_hits:5", "keyspace_misses:1",
        ))
        password = "r" * 32
        with patch.dict("os.environ", {"STAGING_PIPELINE_REDIS_PASSWORD": password}, clear=False), \
                patch.object(staging_metrics_probe, "_run", return_value=info) as run:
            result = staging_metrics_probe._redis("derived-redis")
        self.assertEqual(result["oom_error_count"], 0)
        command = run.call_args.args[0]
        self.assertNotIn(password, " ".join(command))
        self.assertEqual(run.call_args.kwargs["input_text"], password + "\n")

    def test_queue_probe_uses_derived_database_and_rejects_online_snapshot(self):
        with patch.object(staging_metrics_probe, "_run", return_value="0\n") as run:
            result = staging_metrics_probe._queue(
                "binhu-staging-event-pipeline-STG-20260920-01-staging-derived-mysql-1",
                RUN_ID,
                "Staging_EventPipeline",
            )
        self.assertEqual(result, {"pending": 0, "drain_seconds": 0})
        command = run.call_args.args[0]
        self.assertIn("staging-derived-mysql-1", " ".join(command))
        self.assertIn("Staging_EventPipeline", " ".join(command))
        with self.assertRaisesRegex(RuntimeError, "derived database identity"):
            staging_metrics_probe._queue("derived-mysql", RUN_ID, "Staging_s" + "d" * 16 + "_OnlineData")

    def test_kafka_probe_accepts_empty_consumer_groups_before_first_event(self):
        with patch.object(staging_metrics_probe, "_run", return_value=""):
            self.assertEqual(
                staging_metrics_probe._kafka("binhu-staging-event-pipeline-STG-20261006-21", RUN_ID),
                {"lag": 0, "group_count": 2},
            )

    def test_network_cleanup_keeps_frp_and_attached_networks(self):
        frp = {
            "Name": "edge-frp",
            "Driver": "bridge",
            "Internal": False,
            "Labels": {},
            "Containers": {"abc": {"Name": "frpc"}},
        }
        self.assertEqual(staging_network_cleanup._eligible("edge-frp", frp), "attached_containers")

    def test_network_cleanup_only_allows_empty_run_scoped_network(self):
        empty = {
            "Name": "binhu-staging-event-pipeline-STG-20261002-27_internal",
            "Driver": "bridge",
            "Internal": True,
            "Labels": {"binhu.environment": "staging"},
            "Containers": {},
        }
        self.assertIsNone(staging_network_cleanup._eligible(empty["Name"], empty))
        protected = {**empty, "Name": "binhu-staging_internal"}
        self.assertEqual(staging_network_cleanup._eligible(protected["Name"], protected), "protected_name")

    def test_network_cleanup_allows_empty_unprotected_bridge(self):
        empty = {
            "Name": "old-ci-network", "Driver": "bridge", "Internal": False,
            "Labels": {}, "Containers": {},
        }
        self.assertIsNone(staging_network_cleanup._eligible(empty["Name"], empty))

    def test_network_cleanup_does_not_remove_running_or_frp_containers(self):
        source = Path(staging_network_cleanup.__file__).read_text(encoding="utf-8")
        self.assertIn('"docker", "ps", "-aq"', source)
        self.assertIn('"running_container"', source)
        self.assertIn('"frp_container"', source)


class StagingEventPipelineCandidateTests(unittest.TestCase):
    def test_candidate_binds_same_dev_images_snapshot_and_application(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            source = repository / "deploy" / "environments" / "event_pipeline"
            load = repository / "load-tests" / "staging"
            source.mkdir(parents=True)
            load.mkdir(parents=True)
            (source / "PipelineJob.java").write_text("public final class PipelineJob {}\n", encoding="utf-8")
            (source / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
            (load / "locustfile.py").write_text("# synthetic\n", encoding="utf-8")
            (repository / "load-tests" / "requirements.txt").write_text("locust==2.42.6\n", encoding="utf-8")
            jar = Path(directory) / "pipeline-job.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("PipelineJob.class", b"synthetic")
            bundle = Path(directory) / "candidate.tar.gz"
            result = staging_deploy.build(
                repository, bundle, run_id=RUN_ID, commit="a" * 40, images=images(),
                pipeline_jar=jar, candidate_image_digest="sha256:" + "b" * 64,
                configuration_sha256="c" * 64,
                staging_snapshot_id="staging-" + "d" * 16,
                dev_acceptance_run_id="dev-20260919-dualtrack-monitor40",
            )
            self.assertEqual(result["event_pipeline_image_digests"], images())
            verified = staging_deploy.verify(bundle)
            self.assertTrue(verified["verified"])
            self.assertEqual(verified["run_id"], RUN_ID)

    def test_candidate_rejects_mutable_or_cross_environment_identity(self):
        with self.assertRaises(ValueError):
            staging_deploy._identity("dev-20260920-x", staging_deploy.RUN_RE, "run")
        with self.assertRaises(ValueError):
            staging_deploy._identity("latest", staging_deploy.DIGEST_RE, "image")

    def test_staging_gateway_is_fixed_and_separate_from_other_environments(self):
        root = Path(__file__).parents[1] / "environments" / "event_pipeline"
        wrapper = (root / "binhu-staging-event-pipeline-gateway").read_text(encoding="utf-8")
        installer = (root / "install-staging-gateway.sh").read_text(encoding="utf-8")
        gateway = (root / "binhu-staging-event-pipeline-gateway.py").read_text(encoding="utf-8")
        self.assertIn("binhu-staging-deploy", installer)
        self.assertIn("^STG-[0-9]{8}-[0-9]{2}$", wrapper)
        self.assertIn('sys.argv[1] == "sample"', gateway)
        self.assertIn('sys.argv[1] in {"measure", "apply", "switch", "rollback"}', gateway)
        self.assertIn('sys.argv[1] == "run"', gateway)
        self.assertIn('sys.argv[1] == "verify"', gateway)
        self.assertIn('sys.argv[1] == "diagnose-measure"', gateway)
        self.assertIn('sys.argv[1] == "diagnose-prepare"', gateway)
        self.assertIn("measure|apply|switch|rollback", wrapper)
        self.assertIn("run)", wrapper)
        self.assertIn("diagnose-measure)", wrapper)
        self.assertIn("diagnose-prepare)", wrapper)
        self.assertIn("diagnose-measure STG-20261028-01", installer)
        self.assertIn("diagnose-prepare STG-20261028-02", installer)
        self.assertIn("sample)", wrapper)
        self.assertNotIn("down -v", gateway)
        self.assertNotIn("binhu-dev-deploy", installer)
        self.assertNotIn("binhu-production", wrapper + gateway + installer)
        diagnostic = (root / "staging_measure_diagnostic.py").read_text(encoding="utf-8")
        self.assertIn('"error_type"', diagnostic)
        self.assertIn('"error_code"', diagnostic)
        self.assertNotIn("backend.env", diagnostic)
        control = (root / "staging_control.py").read_text(encoding="utf-8")
        self.assertIn('"--production-since"', control)
        self.assertIn('"phase": phase', control)
        self.assertIn('"error_type": type(error).__name__', control)
        self.assertIn('"error_code": error_code', control)
        self.assertIn('item.get("status") == "failed"', gateway)
        self.assertIn('staging_container_conflict', control)
        self.assertIn('staging_image_missing', control)
        self.assertIn('staging_network_attachment_failed', control)
        self.assertIn('staging_container_runtime_failed', control)
        self.assertIn('staging_resource_exhausted', control)
        self.assertIn('staging_resource_limit_unsupported', control)
        self.assertIn('staging_device_driver_missing', control)
        self.assertIn('_ensure_internal_network', control)
        self.assertIn('staging_network_address_pool_exhausted', control)
        self.assertIn('staging_network_firewall_failed', control)
        installer = (root / "install-staging-gateway.sh").read_text(encoding="utf-8")
        self.assertIn('staging_apply_diagnostic.py', installer)
        runtime = (root / "staging_apply_runtime_diagnostic.py").read_text(encoding="utf-8")
        self.assertIn('/srv/binhu-environments/staging-event-pipeline', runtime)
        self.assertIn('"services"', runtime)
        self.assertNotIn('stderr', runtime)
        prepare = (root / "staging_prepare.py").read_text(encoding="utf-8")
        self.assertIn('"phase": "prepare"', prepare)
        self.assertIn('staging_image_identity_mismatch', prepare)
        self.assertIn('"error_code": _safe_prepare_error_code(error)', prepare)
        self.assertIn('staging_image_inspect_failed', prepare)
        self.assertIn('staging_docker_unavailable', prepare)
        self.assertIn('"image_key"', prepare)
        image_diag = (root / "staging_image_diagnostic.py").read_text(encoding="utf-8")
        preload = (root / "staging_schema_registry_preload.py").read_text(encoding="utf-8")
        self.assertIn("APPROVED_IMAGE", preload)
        self.assertIn("docker", preload)
        self.assertIn("archive_sha256", preload)
        self.assertIn("INCOMING_ROOT", preload)
        self.assertIn("image archive path refused", preload)
        self.assertIn("preload-schema-registry", (root.parents[2] / ".github/workflows/install-staging-event-pipeline-gateway.yml").read_text(encoding="utf-8"))
        preload_diag = (root / "staging_schema_registry_preload_diagnostic.py").read_text(encoding="utf-8")
        self.assertIn("preload_evidence_missing", preload_diag)
        self.assertNotIn("stderr", preload_diag)
        with self.assertRaises(ValueError):
            staging_schema_registry_preload.preload("STG-20261001-08", "sha256:" + "0" * 64)
        self.assertIn('"images"', image_diag)
        self.assertNotIn('stderr', image_diag)
        apply_diag = (root / "staging_apply_diagnostic.py").read_text(encoding="utf-8")
        self.assertIn('SAFE_FIELDS', apply_diag)
        self.assertIn('/srv/binhu-environments/staging-event-pipeline', apply_diag)
        self.assertIn('docker_server_available', apply_diag)
        self.assertNotIn('stderr', apply_diag)

    def test_schema_registry_preload_uses_separate_fixed_archive_upload_and_import_phases(self):
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" /
                    "install-staging-event-pipeline-gateway.yml").read_text(encoding="utf-8")
        for marker in (
            "preload_stage=server_archive_prepare",
            "preload_stage=server_archive_upload",
            "preload_stage=server_archive_hash_check",
            "preload_stage=server_preload",
            "server_archive_metadata=",
            "schema-registry-image.tar",
            "chmod 600 -- '$remote_archive'",
        ):
            self.assertIn(marker, workflow)
        self.assertNotIn('cat "$archive" | ssh', workflow)

    def test_measure_diagnostic_action_checks_restricted_key_and_fixed_run(self):
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" /
                    "install-staging-event-pipeline-gateway.yml").read_text(encoding="utf-8")
        self.assertIn("diagnose-measure", workflow)
        self.assertIn("BINHU_STAGING_EVENT_PIPELINE_SSH_KEY", workflow)
        self.assertIn('"${ssh_cmd[@]}" status', workflow)
        self.assertIn('"diagnose-measure $RUN_ID"', workflow)
        self.assertIn("RUN_ID\" == 'STG-20261028-01'", workflow)
        self.assertIn("staging_pipeline_ssh_authentication=passed", workflow)

    def test_prepare_diagnostic_action_checks_restricted_key_and_fixed_run(self):
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" /
                    "install-staging-event-pipeline-gateway.yml").read_text(encoding="utf-8")
        self.assertIn("diagnose-prepare", workflow)
        self.assertIn("BINHU_STAGING_EVENT_PIPELINE_SSH_KEY", workflow)
        self.assertIn('"${ssh_cmd[@]}" status', workflow)
        self.assertIn('"diagnose-prepare $RUN_ID"', workflow)
        self.assertIn('RUN_ID" == \'STG-20261028-02\'', workflow)
        self.assertIn("failure_count", workflow)

    def test_staging_deploy_workflow_returns_success_after_non_apply_actions(self):
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" /
                    "deploy-staging-event-pipeline.yml").read_text(encoding="utf-8")
        self.assertIn('if [[ "$ACTION" == apply ]]; then', workflow)
        self.assertNotIn('[[ "$ACTION" == apply ]] && "${ssh_cmd[@]}"', workflow)

    def test_schema_registry_preload_classifies_fixed_docker_errors(self):
        self.assertEqual(
            staging_schema_registry_preload._docker_error_code(
                "Error response from daemon: no space left on device"
            ),
            "staging_insufficient_disk",
        )
        self.assertEqual(
            staging_schema_registry_preload._docker_error_code(
                "failed to validate image signature"
            ),
            "staging_image_signature_rejected",
        )

    def test_schema_registry_export_and_preload_digest_contracts_agree(self):
        workflows = Path(__file__).parents[2] / ".github" / "workflows"
        approved = staging_schema_registry_preload.APPROVED_IMAGE
        for filename in ("install-dev-event-pipeline-gateway.yml",
                         "install-staging-event-pipeline-gateway.yml"):
            source = (workflows / filename).read_text(encoding="utf-8")
            schema_ids = re.findall(r"sha256:cac935[0-9a-f]{58}", source)
            self.assertTrue(schema_ids, filename)
            self.assertEqual(set(schema_ids), {approved}, filename)

    def test_backend_access_network_is_internal_and_keeps_application_network_untouched(self):
        self.assertEqual(staging_backend_network.NETWORK, "binhu-staging-pipeline-backend")
        self.assertEqual(staging_backend_network.APP_NETWORK, "binhu-staging_internal")
        self.assertEqual(staging_backend_network.LABELS, {
            "binhu.environment": "staging", "binhu.role": "pipeline-backend-access",
        })
        source = Path(staging_backend_network.__file__).read_text(encoding="utf-8")
        self.assertIn('"--internal"', source)
        self.assertIn('"docker", "network", "disconnect"', source)
        self.assertIn("staging_application_network_changed", source)
        self.assertNotIn("binhu-production", source)

    def test_pipeline_job_failure_diagnostics_use_fixed_codes(self):
        source = Path(staging_compose.__file__).with_name("PipelineJob.java").read_text(encoding="utf-8")
        for code in (
            "flink_multiple_distinct_aggregate_keys",
            "flink_sql_function_signature_unmatched",
            "flink_sql_type_mismatch",
            "flink_sql_identifier_missing",
            "flink_sql_feature_unsupported",
            "flink_sql_validation_error_unclassified",
        ):
            self.assertIn(code, source)
        self.assertIn("safeFailureCode(failure)", source)
        self.assertNotIn('types.append(cause.getMessage())', source)

    def test_network_validation_rejects_foreign_members(self):
        valid = {
            "Name": staging_backend_network.NETWORK, "Driver": "bridge", "Internal": True,
            "Labels": dict(staging_backend_network.LABELS),
            "Containers": {},
        }
        with patch.object(staging_backend_network, "inspect", return_value={
            "Config": {"Labels": {"binhu.environment": "production"}},
        }):
            valid["Containers"] = {"foreign": {"Name": "binhu-production-backend-1"}}
            with self.assertRaisesRegex(ValueError, "foreign_member"):
                staging_backend_network.validate_network(valid)

    def test_staging_measure_accepts_valid_connectors_from_an_older_run(self):
        names = (
            "binhu-staging-environment-mysql-1",
            "binhu-staging-redis-1",
            "binhu-staging-event-pipeline-stg-20261007-03-business-bridge-1",
            "binhu-staging-event-pipeline-stg-20261007-03-backend-outbox-relay-1",
        )
        network = {
            "Name": staging_compose.BACKEND_NETWORK,
            "Driver": "bridge",
            "Internal": True,
            "Labels": {"binhu.environment": "staging", "binhu.role": "pipeline-backend-access"},
            "Containers": {str(index): {"Name": name} for index, name in enumerate(names)},
        }
        labels = {
            name: {
                "binhu.environment": "staging",
                "binhu.run_id": "STG-20261007-03",
                "com.docker.compose.project": "binhu-staging-event-pipeline-stg-20261007-03",
                "com.docker.compose.service": "environment-mysql" if name.endswith("environment-mysql-1")
                else "redis" if name.endswith("redis-1")
                else "business-bridge" if name.endswith("business-bridge-1")
                else "backend-outbox-relay",
            }
            for name in names
        }
        labels[names[0]]["com.docker.compose.project"] = "binhu-staging"
        labels[names[1]]["com.docker.compose.project"] = "binhu-staging"
        inspected = [
            {"Name": "/" + name, "State": {"Status": "running"}, "Config": {"Labels": labels[name]}}
            for name in names
        ]

        def docker_json(command):
            return [network] if command[1] == "network" else inspected

        with patch.object(staging_control, "_docker_json", side_effect=docker_json):
            result = staging_control._backend_network("STG-20261027-02")
        self.assertEqual(result, network)

    def test_staging_measure_rejects_mislabeled_old_connectors(self):
        names = (
            "binhu-staging-environment-mysql-1",
            "binhu-staging-redis-1",
            "binhu-staging-event-pipeline-stg-20261007-03-business-bridge-1",
        )
        network = {
            "Name": staging_compose.BACKEND_NETWORK,
            "Driver": "bridge",
            "Internal": True,
            "Labels": {"binhu.environment": "staging", "binhu.role": "pipeline-backend-access"},
            "Containers": {str(index): {"Name": name} for index, name in enumerate(names)},
        }
        inspected = []
        for name in names:
            service = ("environment-mysql" if name.endswith("environment-mysql-1") else
                       "redis" if name.endswith("redis-1") else "business-bridge")
            project = "binhu-staging" if service in {"environment-mysql", "redis"} else \
                "binhu-staging-event-pipeline-stg-20261007-03"
            labels = {
                "binhu.environment": "staging",
                "binhu.run_id": "STG-20261007-03",
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            }
            inspected.append({"Name": "/" + name, "State": {"Status": "running"},
                              "Config": {"Labels": labels}})
        inspected[-1]["Config"]["Labels"]["com.docker.compose.project"] = "binhu-staging-event-pipeline-stg-20261027-02"

        def docker_json(command):
            return [network] if command[1] == "network" else inspected

        with patch.object(staging_control, "_docker_json", side_effect=docker_json):
            with self.assertRaisesRegex(ValueError, "foreign member"):
                staging_control._backend_network("STG-20261027-02")

    def test_staging_connector_identity_requires_all_fixed_labels(self):
        name = "binhu-staging-event-pipeline-stg-20261007-03-business-bridge-1"
        labels = {
            "binhu.environment": "staging",
            "binhu.run_id": "STG-20261007-03",
            "com.docker.compose.project": "binhu-staging-event-pipeline-stg-20261007-03",
            "com.docker.compose.service": "business-bridge",
        }
        self.assertTrue(staging_control._staging_backend_connector(name, labels))
        for key in labels:
            with self.subTest(label=key):
                mismatched = {**labels, key: "production"}
                self.assertFalse(staging_control._staging_backend_connector(name, mismatched))

    def test_staging_gateway_rejects_reseeding_before_starting_seed_container(self):
        gateway = (
            Path(__file__).parents[1]
            / "environments" / "event_pipeline" / "binhu-staging-event-pipeline-gateway.py"
        ).read_text(encoding="utf-8")
        seed = gateway[gateway.index("def seed("):gateway.index("\ndef verify(")]
        self.assertLess(
            seed.index('fail("Staging fixture run already exists")'),
            seed.index('"docker", "run"'),
        )

    def test_retirement_is_fixed_to_authorized_run_and_preserves_volumes(self):
        gateway = (
            Path(__file__).parents[1] / "environments" / "event_pipeline" /
            "binhu-staging-event-pipeline-gateway.py"
        ).read_text(encoding="utf-8")
        wrapper = (
            Path(__file__).parents[1] / "environments" / "event_pipeline" /
            "binhu-staging-event-pipeline-gateway"
        ).read_text(encoding="utf-8")
        install = (
            Path(__file__).parents[1] / "environments" / "event_pipeline" /
            "install-staging-gateway.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('RETIRE_TARGETS = frozenset({"STG-20261027-07", "STG-20261028-03"})', gateway)
        self.assertNotIn('RETIRE_TARGET = "STG-20261007-03"', gateway)
        self.assertIn('RETIRE_SUCCESSOR = "STG-20261027-02"', gateway)
        self.assertIn('FAILED_RETIRE_TARGETS = frozenset({"STG-20261027-04", "STG-20261027-05"})', gateway)
        self.assertIn('def retire_failed(run_id: str)', gateway)
        self.assertIn('"down", "--remove-orphans"', gateway)
        self.assertNotIn('"down", "-v"', gateway)
        self.assertIn("measure|apply|switch|rollback|retire|retire-failed", wrapper)
        self.assertIn("run)", wrapper)
        self.assertIn("retire STG-20261027-07", install)
        self.assertIn("retire STG-20261028-03", install)
        self.assertNotIn("retire STG-20261007-03", install)
        self.assertIn("retire-failed STG-20261027-04", install)

    def test_retirement_allowlist_rejects_other_valid_run_ids(self):
        for run_id in ("STG-20261007-03", "STG-20261029-02"):
            with self.subTest(run_id=run_id), self.assertRaises(SystemExit):
                staging_gateway.retire(run_id)

    def test_retirement_sudoers_has_no_wildcard(self):
        install = (
            Path(__file__).parents[1] / "environments" / "event_pipeline" /
            "install-staging-gateway.sh"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(install, r"retire \*")

    def test_retirement_rejects_foreign_run_without_docker_access(self):
        with self.assertRaises(SystemExit):
            staging_gateway.retire("STG-20261027-02")

    def test_failed_retirement_rejects_foreign_run_without_docker_access(self):
        with self.assertRaises(SystemExit):
            staging_gateway.retire_failed("STG-20261027-02")

    def test_failed_retirement_requires_matching_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            evidence = state / "evidence" / "STG-20261027-05" / "apply-run"
            evidence.mkdir(parents=True)
            (evidence / "failure.json").write_text(json.dumps({
                "environment": "staging", "run_id": "STG-20261027-05",
                "acceptance": "failed", "error_code": "flink_rest_http_400",
            }), encoding="utf-8")
            with patch.object(staging_gateway, "STATE", state):
                result = staging_gateway._failed_retirement_evidence("STG-20261027-05")
            self.assertEqual(result[0]["path"], "apply-run/failure.json")
            self.assertRegex(result[0]["sha256"], r"^[0-9a-f]{64}$")

    def test_failed_retirement_rejects_missing_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "evidence" / "STG-20261027-05").mkdir(parents=True)
            with patch.object(staging_gateway, "STATE", state):
                with self.assertRaisesRegex(ValueError, "staging_retirement_failure_evidence_missing"):
                    staging_gateway._failed_retirement_evidence("STG-20261027-05")

    def test_realistic_load_workflow_runs_fixed_profiles_and_keeps_production_outside_gateway(self):
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" /
                    "run-staging-realistic-load.yml").read_text(encoding="utf-8")
        for marker in (
            '"run $CANDIDATE_RUN_ID $RUN_ID"', 'sample $CANDIDATE_RUN_ID $RUN_ID $monitor_start_epoch$sample_suffix',
            "options: [low20, full75]", '--users "$users"', '--spawn-rate "$spawn_rate"',
            '--run-time "${duration_seconds}s"',
            "/staging/api/query/", "staging-samples.jsonl", "derived_queue",
            "waive_4gib_memory_gate", "default: false", "staging.load_monitor observe",
            "production_stop_thresholds", "health_failures",
            "production_mode", "production_pause_run_id", "PROD-PAUSE-20261029-02",
        ):
            self.assertIn(marker, workflow)
        profile = (Path(__file__).parents[2] / "load-tests" / "staging" / "profile.py").read_text(encoding="utf-8")
        self.assertIn('"low20": LoadProfile(', profile)
        self.assertIn("name=\"low20\", users=20, spawn_rate=2, duration_seconds=600", profile)
        self.assertNotIn("BINHU_DEPLOY_SSH_KEY", workflow)
        self.assertNotIn("binhu-deploy@", workflow)

    def test_realistic_load_wrapper_requires_the_authorized_pause_run(self):
        wrapper = (Path(__file__).parents[1] / "environments" / "event_pipeline" /
                   "binhu-staging-event-pipeline-gateway").read_text(encoding="utf-8")
        gateway = (Path(__file__).parents[1] / "environments" / "event_pipeline" /
                   "binhu-staging-event-pipeline-gateway.py").read_text(encoding="utf-8")
        self.assertIn('"${#parts[@]}" -eq 4 || "${#parts[@]}" -eq 6', wrapper)
        self.assertIn('"${parts[1]}" == STG-20261029-02', wrapper)
        self.assertIn('"${parts[4]}" == production_paused', wrapper)
        self.assertIn('"${parts[5]}" == PROD-PAUSE-20261029-02', wrapper)
        self.assertIn('sys.argv[6] == "PROD-PAUSE-20261029-02"', gateway)

    def test_rollback_workflow_uses_only_fixed_staging_command(self):
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" /
                    "rollback-staging-event-pipeline.yml").read_text(encoding="utf-8")
        self.assertIn('"rollback $RUN_ID"', workflow)
        self.assertIn("data_consistency_verified", workflow)
        self.assertIn("legacy_path_healthy", workflow)
        self.assertNotIn("BINHU_DEPLOY_SSH_KEY", workflow)


if __name__ == "__main__":
    unittest.main()
