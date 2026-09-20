import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from deploy.environments.event_pipeline import (
    staging_compose, staging_control, staging_deploy, staging_metrics_probe, staging_prepare,
)


RUN_ID = "STG-20260920-01"


def images():
    return {name: "sha256:" + str(index) * 64
            for index, name in enumerate(sorted(staging_compose.IMAGE_KEYS), start=1)}


class StagingEventPipelineComposeTests(unittest.TestCase):
    def test_complete_model_is_isolated_and_bounded(self):
        spec = staging_compose.compose(images(), RUN_ID)
        self.assertEqual(spec["name"], "binhu-staging-event-pipeline-stg-20260920-01")
        self.assertTrue(spec["networks"]["internal"]["internal"])
        self.assertEqual(spec["networks"]["backend"], {
            "external": True, "name": "binhu-staging_internal",
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
            self.assertEqual(service["environment"]["KAFKA_AUTO_CREATE_TOPICS_ENABLE"], "false")
            self.assertIn(f"{name}-data:/var/lib/kafka/data", service["volumes"])
            self.assertIn(f"{name}-secrets-tmpfs:/etc/kafka/secrets", service["volumes"])
            self.assertIn(f"{name}-config-tmpfs:/mnt/shared/config", service["volumes"])
            for suffix in ("secrets-tmpfs", "config-tmpfs"):
                volume = spec["volumes"][f"{name}-{suffix}"]
                self.assertEqual(volume["driver_opts"]["type"], "tmpfs")
        self.assertEqual(
            spec["services"]["schema-registry"]["environment"]["REGISTRY_KAFKASQL_TOPIC"],
            "staging.registry.storage.v1",
        )
        self.assertNotIn("dev.task.events", str(spec))
        self.assertNotIn("binhu-development", str(spec))
        self.assertNotIn("binhu-production", str(spec))

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
