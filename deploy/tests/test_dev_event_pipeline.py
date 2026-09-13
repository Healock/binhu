import asyncio
import copy
import json
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from deploy.environments.event_pipeline.runtime import configuration, cache_result
from deploy.environments.event_pipeline.prepare import compose
from deploy.environments.event_pipeline.flink_sql import render
from deploy.environments.event_pipeline.services.kafka_envelope import delivery_topic
from deploy.environments.event_pipeline.services.kafka_event_contract import validate_event, EventContractError
from deploy.environments.event_pipeline.services.kafka_relay import KafkaRelay, Delivery
from deploy.environments.event_pipeline.services.kafka_delivery_store import MySQLDeliveryStore
from deploy.environments.event_pipeline.services.derived_revision_cache import RevisionCache, CacheContractError
from deploy.environments.event_pipeline import schema_registry
from deploy.environments.event_pipeline import registry_runtime
from deploy.environments.event_pipeline import checkpoint
from deploy.environments.event_pipeline import control
from deploy.environments.event_pipeline import flink_compose
from deploy.environments.event_pipeline import kafka_compose
from deploy.environments.event_pipeline.business_bridge import event_to_task_event
from deploy.environments.event_pipeline.verify import fixture, acceptance_event_ids


def settings():
    return {"APP_ENVIRONMENT": "development", "DEV_RUN_ID": "dev-test-1",
            "MYSQL_HOST": "dev-derived-mysql", "MYSQL_DATABASE": "Dev_EventPipeline",
            "MYSQL_USER": "dev_pipeline", "MYSQL_PASSWORD": "a" * 48,
            "REDIS_HOST": "dev-derived-redis", "REDIS_PASSWORD": "b" * 48,
            "KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9092,kafka-2:9092,kafka-3:9092"}


def event():
    return {"schema_version": 1, "event_id": "11111111-1111-4111-8111-111111111111",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "event_type": "task.saved", "task_id": "t_fullchain:1", "source_id": 1,
            "revision": 1, "changed_fields": ["check_result"],
            "timestamp": "2026-09-10T00:00:00Z", "environment": "development", "run_id": "dev-test-1"}


class BusinessBridgeTests(unittest.TestCase):
    def test_business_bridge_strips_body_and_maps_stable_metadata(self):
        source = {"event_id": "11111111-1111-4111-8111-111111111111",
                  "event_type": "online.task.changed", "aggregate_id": "全链条:opaque-row-key",
                  "aggregate_revision": 7, "occurred_at": "2026-09-12T00:00:00Z",
                  "detail": "must never be copied"}
        converted = event_to_task_event(source, "dev-acceptance-01")
        self.assertIsNotNone(converted)
        self.assertEqual(converted["task_id"].split(":")[0], "t_fullchain")
        self.assertEqual(converted["revision"], 7)
        self.assertEqual(converted["event_id"], converted["operation_id"])
        self.assertNotIn("detail", converted)
        self.assertEqual(event_to_task_event(source, "dev-acceptance-01"), converted)

    def test_business_bridge_rejects_unknown_events(self):
        source = {"event_id": "11111111-1111-4111-8111-111111111111",
                  "event_type": "online.task.changed", "aggregate_id": "全链条:key",
                  "aggregate_revision": 1}
        self.assertIsNone(event_to_task_event({**source, "event_type": "unknown"}, "dev-x"))
        self.assertIsNone(event_to_task_event({**source, "aggregate_id": "unknown:key"}, "dev-x"))

    def test_acceptance_fixture_nonce_prevents_ledger_reuse(self):
        first = fixture("dev-test-1", 3, nonce="accept-a")
        second = fixture("dev-test-1", 3, nonce="accept-b")
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_acceptance_verification_scopes_delivery_ids_to_current_attempt(self):
        ids = acceptance_event_ids("dev-test-1", 7, "accept-a")
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(set(ids).isdisjoint(acceptance_event_ids("dev-test-1", 7, "accept-b")))


class ContractTests(unittest.TestCase):
    def test_eventbus_identity_patch_covers_all_base_services(self):
        source = """name: binhu-development-eventbus
services:
  kafka-1:
    image: kafka
    labels:
      binhu.shadow: \"true\"
      binhu.development.run_id: dev-test-1
    networks: [internal]
    volumes: [kafka-1-data:/var/lib/kafka/data]
  kafka-2:
    image: kafka
    labels:
      binhu.shadow: \"true\"
      binhu.development.run_id: dev-test-1
    networks: [internal]
    volumes: [kafka-2-data:/var/lib/kafka/data]
  kafka-3:
    image: kafka
    labels:
      binhu.shadow: \"true\"
      binhu.development.run_id: dev-test-1
    networks: [internal]
    volumes: [kafka-3-data:/var/lib/kafka/data]
  schema-registry:
    image: registry
    labels:
      binhu.shadow: \"true\"
      binhu.development.run_id: dev-test-1
networks:
  internal:
    internal: true
volumes:
  kafka-1-data: {}
  kafka-2-data: {}
  kafka-3-data: {}
"""
        patched = kafka_compose.patch_runtime_guardrails(source)
        self.assertEqual(patched.count("binhu.environment: development"), 4)
        self.assertNotIn("binhu.shadow", patched)
        self.assertNotIn("    tmpfs:", patched)
        self.assertEqual(patched.count("    logging:"), 3)
        self.assertEqual(patched.count("        max-size: 5m"), 3)
        self.assertEqual(patched.count("        max-file: 2"), 3)
        self.assertEqual(patched.count("    driver: local"), 6)
        self.assertEqual(patched.count("      type: tmpfs"), 6)
        for service in kafka_compose.BROKER_SERVICES:
            for suffix, definition in kafka_compose.BROKER_EPHEMERAL_STORAGE.items():
                volume = f"{service}-{suffix}"
                self.assertIn(f"      - {volume}:{definition['target']}", patched)
                self.assertIn(f"  {volume}:\n", patched)
                self.assertIn(f"      o: {definition['options']}", patched)
        self.assertEqual(kafka_compose.patch_runtime_guardrails(patched), patched)
        self.assertIn("binhu.development.run_id: dev-test-1", patched)

    def test_eventbus_identity_patch_rejects_other_services_and_wrong_environment(self):
        source = """services:
  kafka-1:
    labels:
      binhu.environment: production
"""
        with self.assertRaises(ValueError):
            kafka_compose.patch_runtime_guardrails(source)
        complete = "services:\n" + "".join(
            f"  {service}:\n    labels:\n      binhu.environment: development\n"
            for service in kafka_compose.BASE_SERVICES
        )
        with self.assertRaises(ValueError):
            kafka_compose.patch_runtime_guardrails(
                complete + "  unexpected-worker:\n    labels:\n      binhu.environment: development\n"
            )

    def test_eventbus_comparison_ignores_only_approved_runtime_changes(self):
        base = {
            "name": kafka_compose.PROJECT,
            "services": {
                service: {
                    "image": "sha256:" + "a" * 64,
                    "labels": {
                        "binhu.shadow": "true",
                        "binhu.development.run_id": "dev-test-1",
                    },
                }
                for service in kafka_compose.BASE_SERVICES
            },
        }
        target = copy.deepcopy(base)
        for service in kafka_compose.BASE_SERVICES:
            target["services"][service]["labels"].pop("binhu.shadow")
            target["services"][service]["labels"]["binhu.environment"] = "development"
        base["volumes"] = {}
        target["volumes"] = {}
        for service in kafka_compose.BROKER_SERVICES:
            data_mount = {
                "type": "volume",
                "source": f"{service}-data",
                "target": "/var/lib/kafka/data",
            }
            base["services"][service]["volumes"] = [data_mount]
            target["services"][service]["volumes"] = [copy.deepcopy(data_mount)]
            base["volumes"][f"{service}-data"] = {}
            target["volumes"][f"{service}-data"] = {}
            for suffix, definition in kafka_compose.BROKER_EPHEMERAL_STORAGE.items():
                name = f"{service}-{suffix}"
                target["services"][service]["volumes"].append(
                    {"type": "volume", "source": name, "target": definition["target"]}
                )
                target["volumes"][name] = kafka_compose._named_tmpfs_volume_definition(
                    definition["options"]
                )
        self.assertEqual(
            kafka_compose._without_approved_labels(base),
            kafka_compose._without_approved_tmpfs(
                kafka_compose._without_approved_labels(target)
            ),
        )
        self.assertNotEqual(
            kafka_compose._model_sha256(base),
            kafka_compose._model_sha256(target),
        )
        changed = copy.deepcopy(target)
        changed["services"]["kafka-1"]["image"] = "sha256:" + "b" * 64
        self.assertNotEqual(
            kafka_compose._without_approved_labels(base),
            kafka_compose._without_approved_labels(changed),
        )
        self.assertNotEqual(
            kafka_compose._model_sha256(base),
            kafka_compose._model_sha256(changed),
        )
        unexpected_tmpfs = copy.deepcopy(target)
        unexpected_tmpfs["volumes"]["kafka-1-secrets-tmpfs"]["driver_opts"]["o"] = "size=1"
        with self.assertRaises(ValueError):
            kafka_compose._without_approved_tmpfs(unexpected_tmpfs)

    def test_eventbus_runtime_patch_rejects_partial_or_unknown_tmpfs(self):
        complete = "services:\n" + "".join(
            f"  {service}:\n"
            "    labels:\n"
            "      binhu.environment: development\n"
            "    networks: [internal]\n"
            + (f"    volumes: [{service}-data:/var/lib/kafka/data]\n" if service in kafka_compose.BROKER_SERVICES else "")
            for service in kafka_compose.BASE_SERVICES
        ) + "volumes:\n" + "".join(
            f"  {service}-data: {{}}\n" for service in kafka_compose.BROKER_SERVICES
        )
        partial = complete.replace(
            "  kafka-1:\n    labels:",
            "  kafka-1:\n    tmpfs:\n      - /etc/kafka/secrets:size=1m\n    labels:",
        )
        with self.assertRaises(ValueError):
            kafka_compose.patch_runtime_guardrails(partial)

    def test_eventbus_runtime_verify_requires_named_tmpfs_backed_volumes(self):
        containers = []
        for service in kafka_compose.BROKER_SERVICES:
            mounts = [
                {
                    "Type": "volume",
                    "Name": f"{kafka_compose.PROJECT}_{service}-data",
                    "Destination": "/var/lib/kafka/data",
                    "RW": True,
                }
            ]
            for suffix, definition in kafka_compose.BROKER_EPHEMERAL_STORAGE.items():
                mounts.append(
                    {
                        "Type": "volume",
                        "Name": f"{kafka_compose.PROJECT}_{service}-{suffix}",
                        "Destination": definition["target"],
                        "RW": True,
                    }
                )
            containers.append(
                {
                    "Name": f"/{kafka_compose.PROJECT}-{service}-1",
                    "Id": service,
                    "Image": "sha256:" + "a" * 64,
                    "State": {"Running": True},
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": kafka_compose.PROJECT,
                            "com.docker.compose.service": service,
                            "binhu.environment": "development",
                        }
                    },
                    "NetworkSettings": {"Networks": {kafka_compose.NETWORK: {}}},
                    "HostConfig": {"LogConfig": {"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}}},
                    "Mounts": mounts,
                }
            )

        def checked(argv, cwd=None):
            if argv[:2] == ["docker", "inspect"]:
                return json.dumps(containers)
            if argv[:3] == ["docker", "network", "inspect"]:
                return json.dumps(
                    [{
                        "Id": "network-id",
                        "Internal": True,
                        "Labels": {"com.docker.compose.project": kafka_compose.PROJECT},
                    }]
                )
            if argv[:3] == ["docker", "volume", "inspect"]:
                name = argv[3]
                if name.endswith("-data"):
                    return json.dumps(
                        [{"Labels": {"com.docker.compose.project": kafka_compose.PROJECT}}]
                    )
                suffix = next(
                    key for key in kafka_compose.BROKER_EPHEMERAL_STORAGE if name.endswith(key)
                )
                definition = kafka_compose.BROKER_EPHEMERAL_STORAGE[suffix]
                return json.dumps(
                    [{
                        "Driver": "local",
                        "Labels": {"com.docker.compose.project": kafka_compose.PROJECT},
                        "Options": kafka_compose._named_tmpfs_volume_definition(
                            definition["options"]
                        )["driver_opts"],
                    }]
                )
            if argv[:2] == ["docker", "exec"] and argv[3:7] == [
                "stat", "-f", "-c", "%T"
            ]:
                return "tmpfs\ntmpfs\n"
            if argv[:2] == ["docker", "exec"] and argv[3:6] == ["stat", "-c", "%u:%g:%a"]:
                return "1000:1000:750\n1000:1000:750\n"
            raise AssertionError(argv)

        with patch.object(
            kafka_compose,
            "measure",
            return_value={"requires_update": False, "before_sha256": "definition"},
        ), patch.object(kafka_compose, "_checked", side_effect=checked):
            result = kafka_compose.verify_runtime()
        self.assertTrue(result["verified"])
        self.assertEqual(set(result["brokers"]), set(kafka_compose.BROKER_SERVICES))

        containers[0]["Mounts"][1]["Name"] = "a" * 64
        with patch.object(
            kafka_compose,
            "measure",
            return_value={"requires_update": False, "before_sha256": "definition"},
        ), patch.object(kafka_compose, "_checked", side_effect=checked), self.assertRaises(ValueError):
            kafka_compose.verify_runtime()

    def test_all_generated_event_pipeline_containers_have_development_label(self):
        specifications = (
            compose({name: "sha256:" + "a" * 64 for name in ("mysql", "redis", "worker")}),
            registry_runtime.specification("sha256:" + "a" * 64),
            flink_compose.specification(
                "sha256:" + "a" * 64,
                Path("/srv/binhu-environments/build-dev-pipeline-a9409fce"),
            ),
        )
        for specification in specifications:
            for service, definition in specification["services"].items():
                with self.subTest(service=service):
                    self.assertEqual(
                        definition.get("labels", {}).get("binhu.environment"),
                        "development",
                    )

    def test_failed_schema_check_prevents_start_and_keeps_each_attempt(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(control, "ROOT", Path(tmp)), patch.object(control, "measure", return_value={}), patch('time.time_ns', return_value=123):
            with patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="schema unavailable")) as command:
                for attempt in range(2):
                    with self.assertRaises(ValueError):
                        control.apply()
                self.assertEqual(command.call_count, 2)
                self.assertTrue(all("up" not in call.args[0] for call in command.call_args_list))
            evidence = list(Path(tmp).glob("apply-evidence-*"))
            self.assertEqual(len(evidence), 2)
            self.assertTrue(all((entry / "schema-check.log").exists() for entry in evidence))

    def test_checkpoint_repair_rejects_shared_or_foreign_volume(self):
        items = [{"Name": name, "Config": {"Labels": {"com.docker.compose.project": "binhu-development-flink",
                  "binhu.environment": "development"}},
                  "Mounts": [{"Name": checkpoint.VOLUME, "Destination": checkpoint.TARGET, "RW": True}],
                  "NetworkSettings": {"Networks": {"binhu-development-eventbus_internal": {}}}}
                 for name in (checkpoint.JM, checkpoint.TM)]
        self.assertEqual(len(checkpoint.inspect_holders(items)), 2)
        foreign = copy.deepcopy(items[0])
        foreign["Name"] = "binhu-backend"
        with self.assertRaises(ValueError):
            checkpoint.inspect_holders(items + [foreign])
        items[0]["Mounts"][0]["Destination"] = "/unexpected"
        with self.assertRaises(ValueError):
            checkpoint.inspect_holders(items)

    def test_flink_compose_has_explicit_development_identity(self):
        spec = flink_compose.specification(
            "sha256:" + "a" * 64,
            Path("/srv/binhu-environments/build-dev-pipeline-a9409fce"),
        )
        self.assertEqual(spec["name"], "binhu-development-flink")
        self.assertEqual(set(spec["services"]), {"jobmanager", "taskmanager"})
        for service in spec["services"].values():
            self.assertEqual(service["labels"], {"binhu.environment": "development"})
            self.assertEqual(service["environment"]["APP_ENVIRONMENT"], "development")
            self.assertEqual(set(service["networks"]), {"internal"})
            self.assertEqual(service["pids_limit"], 256)
            self.assertEqual(service["volumes"][0]["source"], "flink-checkpoints")
        self.assertEqual(
            spec["volumes"]["flink-checkpoints"]["name"],
            "binhu-development_flink-checkpoints",
        )
        with self.assertRaises(ValueError):
            flink_compose.specification("flink:latest", Path("/srv/binhu-environments/build-dev-pipeline-a9409fce"))

    def test_flink_compose_patch_rejects_unapproved_changes(self):
        expected = flink_compose.specification(
            "sha256:" + "a" * 64,
            Path("/srv/binhu-environments/build-dev-pipeline-a9409fce"),
        )
        without_labels = flink_compose._without_environment_labels(expected)
        self.assertNotIn("labels", without_labels["services"]["jobmanager"])
        changed = copy.deepcopy(without_labels)
        changed["services"]["jobmanager"]["mem_limit"] = "1g"
        self.assertNotEqual(
            flink_compose._without_environment_labels(changed),
            flink_compose._without_environment_labels(expected),
        )

    def test_schema_registry_identity_and_closed_body(self):
        import json
        schema = schema_registry.schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["environment"], {"const": "development"})
        response = {"schemaType": "JSON", "schema": json.dumps(schema), "id": 1, "version": 1}
        with patch.object(schema_registry, "request", return_value=response):
            self.assertTrue(schema_registry.verify()["verified"])
        response["schema"] = "{}"
        with patch.object(schema_registry, "request", return_value=response), self.assertRaises(ValueError):
            schema_registry.verify()
        with self.assertRaises(ValueError):
            schema_registry.request("/subjects/production/versions")
        with self.assertRaises(ValueError):
            schema_registry.NoRedirect().redirect_request(None, None, 302, None, None, "https://external.invalid")

    def test_apicurio_endpoint_and_durable_dev_registry(self):
        self.assertEqual(schema_registry.BASE, "http://schema-registry:8080/apis/ccompat/v7")
        spec = registry_runtime.specification("sha256:" + "a" * 64)
        self.assertEqual(set(spec["services"]), {"schema-registry"})
        service = spec["services"]["schema-registry"]
        self.assertNotIn("ports", service)
        self.assertNotIn("volumes", service)
        env = service["environment"]
        self.assertEqual(env["REGISTRY_KAFKASQL_TOPIC"], "dev.registry.storage.v1")
        self.assertEqual(env["REGISTRY_KAFKASQL_TOPIC_AUTO_CREATE"], "false")
        self.assertIn("ActiveProcessorCount=2", env["JAVA_OPTIONS"])
        self.assertIn("ExitOnOutOfMemoryError", env["JAVA_OPTIONS"])
        with self.assertRaises(ValueError):
            registry_runtime.specification("apicurio:latest")

    def test_registry_refuses_foreign_services_and_networks(self):
        net = {"Internal": True, "Labels": {"com.docker.compose.project": registry_runtime.PROJECT}, "Containers": {}}
        def item(name, service):
            return {"Name": name, "Config": {"Labels": {"com.docker.compose.project": registry_runtime.PROJECT,
                    "com.docker.compose.service": service}}, "NetworkSettings": {"Networks": {registry_runtime.NETWORK: {}}},
                    "State": {"Running": True}}
        registry = item(registry_runtime.REGISTRY, "schema-registry")
        broker = item(registry_runtime.BROKER, "kafka-1")
        registry_runtime.validate_network(net, registry, broker)
        broker["NetworkSettings"]["Networks"]["production"] = {}
        with self.assertRaises(ValueError):
            registry_runtime.validate_network(net, registry, broker)

    def test_every_target_is_fixed_and_environment_guarded(self):
        valid = settings()
        self.assertEqual(configuration(valid)["DEV_RUN_ID"], "dev-test-1")
        for key in valid:
            changed = {**valid, key: "production"}
            with self.subTest(key=key), self.assertRaises(ValueError):
                configuration(changed)

    def test_text_and_external_events_cannot_enter_task_topic(self):
        for bad in ({**event(), "name": "synthetic-person"},
                    {**event(), "event_type": "photo.writeback.requested"},
                    {**event(), "environment": "shadow"},
                    {**event(), "run_id": "dev-x'; DROP TABLE x"}):
            with self.assertRaises(EventContractError):
                validate_event(bad)
        self.assertEqual(delivery_topic("task.saved", "events"), "dev.task.events.v1")
        with self.assertRaises(EventContractError):
            delivery_topic("venue.sync.requested", "events")

    def test_duplicate_changed_fields_and_unsafe_integer_rejected(self):
        for update in ({"changed_fields": ["address", "address"]}, {"revision": True}, {"revision": 2**63}):
            with self.assertRaises(EventContractError):
                validate_event({**event(), **update})

    def test_store_run_validation_precedes_database_access(self):
        for bad in ("production", "dev-", "dev-" + "x" * 65, None):
            with self.assertRaises(ValueError):
                MySQLDeliveryStore(None, run_id=bad)

    def test_compose_has_no_host_ports_or_old_storage(self):
        spec = compose({name: "sha256:" + "a" * 64 for name in ("mysql", "redis", "worker")})
        for service in spec["services"].values():
            self.assertNotIn("ports", service)
            self.assertNotIn("privileged", service)
            self.assertIn("mem_limit", service)
            self.assertIn("cpus", service)
        self.assertFalse(any(v.get("external") for v in spec["volumes"].values()))
        self.assertIn("max:1024M", " ".join(spec["services"]["dev-derived-mysql"]["command"]))
        self.assertEqual(spec["services"]["relay"]["depends_on"]["dev-derived-mysql"]["condition"], "service_healthy")
        self.assertIn("-h127.0.0.1", spec["services"]["dev-derived-mysql"]["healthcheck"]["test"])
        with self.assertRaises(ValueError):
            compose({"mysql": "mysql:latest"})

    def test_sql_has_run_filter_checkpoint_and_idempotent_sink_key(self):
        sql = render(settings())
        for expected in ("MAX(revision)", "run_id = 'dev-test-1'", "environment = 'development'",
                         "PRIMARY KEY (run_id, task_id, source_id)", "execution.checkpointing.interval",
                         "dev.task.events.v1", "Dev_EventPipeline"):
            self.assertIn(expected, sql)
        self.assertIn(
            "autoReconnect=true&maxReconnects=3&initialTimeout=2&tcpKeepAlive=true&connectTimeout=5000&socketTimeout=15000",
            sql,
        )
        self.assertNotIn("shadow", sql)
        with self.assertRaises(ValueError):
            render({**settings(), "MYSQL_PASSWORD": "x';secret"})


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_then_failed_commit_does_not_mark_transport_retry(self):
        delivery = Delivery("event", b"{}", b"key", "lease", 1, "events")
        store = AsyncMock()
        store.claim.return_value = delivery
        store.finish.side_effect = RuntimeError("commit failed")
        producer = AsyncMock()
        with self.assertRaises(RuntimeError):
            await KafkaRelay(store, producer).run_once()
        store.finish.assert_awaited_once()
        self.assertEqual(store.finish.call_args.kwargs["status"], "published")

    async def test_timeout_is_bounded_and_last_attempt_goes_to_dlq(self):
        store, producer = AsyncMock(), AsyncMock()
        store.claim.return_value = Delivery("event", b"{}", b"key", "lease", 5, "events")
        producer.send_and_wait.side_effect = TimeoutError("secret must not escape")
        store.finish.return_value = True
        self.assertEqual(await KafkaRelay(store, producer).run_once(), "dlq_pending")
        self.assertEqual(store.finish.call_args.kwargs["error_code"], "transport_timeout")

    async def test_lost_lease_cannot_claim_success(self):
        store, producer = AsyncMock(), AsyncMock()
        store.claim.return_value = Delivery("event", b"{}", b"key", "lease", 1, "events")
        store.finish.return_value = False
        self.assertEqual(await KafkaRelay(store, producer).run_once(), "lease_lost")

    async def test_cache_preserves_int64_revision_and_checks_namespace(self):
        redis = AsyncMock()
        redis.eval.return_value = 1
        cache = RevisionCache(redis, "dev-test-1")
        result = cache_result("t_fullchain:1", 1, 2**63 - 1)
        self.assertEqual(await cache.put(result), "updated")
        args = redis.eval.call_args.args
        self.assertIn("binhu:development:dev-test-1", args[2])
        self.assertEqual(args[4], str(2**63 - 1))
        for code, state in ((0, "stale"), (2, "duplicate"), (-1, "conflict")):
            redis.eval.return_value = code
            self.assertEqual(await cache.put(result), state)
        with self.assertRaises(CacheContractError):
            RevisionCache(redis, "shadow-test")


if __name__ == "__main__":
    unittest.main()
