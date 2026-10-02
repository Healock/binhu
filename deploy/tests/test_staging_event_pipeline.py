import json
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile
import subprocess

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


class StagingEventPipelineComposeTests(unittest.TestCase):
    def test_network_address_pool_exhaustion_signature_is_classified(self):
        result = subprocess.CompletedProcess(
            ["docker", "network", "create"], 1, "",
            "Error response from daemon: could not find an available, non-overlapping IPv4 address pool among the defaults to assign to the network",
        )
        with patch("deploy.environments.event_pipeline.staging_control.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ValueError, "staging_network_address_pool_exhausted"):
                staging_control._run(["docker", "network", "create"])

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

    def test_metrics_probe_is_aggregate_fixed_and_excludes_production(self):
        source = Path(staging_metrics_probe.__file__).read_text(encoding="utf-8")
        for metric in (
            "Innodb_deadlocks", "Innodb_row_lock_current_waits", "keyspace_hits",
            "checkpoint_completed", "_kafka_event_delivery", "restart_count",
            "pool_count", "/api/admin/ops/performance",
        ):
            self.assertIn(metric, source)
        self.assertIn('"production": None', source)
        self.assertNotIn("binhu-mysql", source)

    def test_backend_pool_probe_passes_private_password_only_over_stdin(self):
        payload = {"pool_count": 8, "usage_ratio": .5, "used": 20, "max_size": 40}
        with patch.dict("os.environ", {"STAGING_LOAD_TEST_PASSWORD": "x" * 32}, clear=False), \
                patch.object(staging_metrics_probe, "_run", return_value=json.dumps(payload)) as run:
            self.assertEqual(
                staging_metrics_probe._backend_pool("binhu-staging-backend-1", RUN_ID), payload,
            )
        command = run.call_args.args[0]
        self.assertNotIn("x" * 32, " ".join(command))
        self.assertEqual(run.call_args.kwargs["input_text"], "x" * 32 + "\n")

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
        self.assertIn('in {"measure", "apply", "sample", "rollback"}', gateway)
        self.assertIn('sys.argv[1] == "run"', gateway)
        self.assertIn('sys.argv[1] == "verify"', gateway)
        self.assertIn("measure|apply|run|sample|rollback", wrapper)
        self.assertNotIn("down -v", gateway)
        self.assertNotIn("binhu-dev-deploy", installer)
        self.assertNotIn("binhu-production", wrapper + gateway + installer)
        diagnostic = (root / "staging_measure_diagnostic.py").read_text(encoding="utf-8")
        self.assertIn('"error_type"', diagnostic)
        self.assertIn('"error_code"', diagnostic)
        self.assertNotIn("backend.env", diagnostic)
        control = (root / "staging_control.py").read_text(encoding="utf-8")
        self.assertIn('"phase": phase', control)
        self.assertIn('"error_type": type(error).__name__', control)
        self.assertIn('"error_code": error_code', control)
        self.assertIn('payload.get("status") == "failed"', gateway)
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

    def test_realistic_load_workflow_runs_75_users_and_keeps_production_outside_gateway(self):
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" /
                    "run-staging-realistic-load.yml").read_text(encoding="utf-8")
        for marker in (
            '"run $RUN_ID"', '"sample $RUN_ID"', "--users 75", "--run-time 30m",
            "/staging/api/query/", "staging-samples.jsonl", "derived_queue",
        ):
            self.assertIn(marker, workflow)
        self.assertNotIn("BINHU_DEPLOY_SSH_KEY", workflow)
        self.assertNotIn("binhu-deploy@", workflow)

    def test_rollback_workflow_uses_only_fixed_staging_command(self):
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" /
                    "rollback-staging-event-pipeline.yml").read_text(encoding="utf-8")
        self.assertIn('"rollback $RUN_ID"', workflow)
        self.assertIn("data_consistency_verified", workflow)
        self.assertIn("legacy_path_healthy", workflow)
        self.assertNotIn("BINHU_DEPLOY_SSH_KEY", workflow)


if __name__ == "__main__":
    unittest.main()
