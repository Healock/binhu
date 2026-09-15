import asyncio
import copy
import json
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from deploy.environments.event_pipeline.runtime import configuration, cache_result, backend_relay_configuration
from deploy.environments.event_pipeline.prepare import compose, validate_existing_container_identity
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
from deploy.environments.event_pipeline import prepare as event_prepare
from deploy.environments.event_pipeline import runtime as event_runtime
from deploy.environments.event_pipeline.dual_track_monitor import build_report, write_cycle
from deploy.environments.event_pipeline.business_bridge import event_to_task_event
from deploy.environments.event_pipeline.verify import fixture, acceptance_event_ids
from deploy.environments.event_pipeline.services.backend_outbox_relay import BackendOutboxRelay


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


class FlinkStateContractTests(unittest.TestCase):
    def test_pipeline_job_builds_one_statement_set_with_two_insert_branches(self):
        source = (Path(__file__).parents[1] / "environments" / "event_pipeline" / "PipelineJob.java").read_text(encoding="utf-8")
        self.assertIn("createStatementSet()", source)
        self.assertIn("addInsertSql(command)", source)
        self.assertIn("statementSet.execute()", source)
        self.assertNotIn("statementSet.addInsertSql(command);\n                    }\n                    table.executeSql(command)", source)

    def test_control_failure_detail_is_safe_and_keeps_flink_gate_reason(self):
        self.assertEqual(
            control.safe_control_failure_detail(
                ValueError("Flink runtime missing INSERT sink: dev_task_metadata")
            ),
            "Flink runtime missing INSERT sink: dev_task_metadata",
        )
        self.assertEqual(
            control.safe_control_failure_detail(
                ValueError("password=super-secret-value")
            ),
            "ValueError",
        )

    def test_pipeline_job_declares_stable_operator_uids(self):
        source = Path("deploy/environments/event_pipeline/PipelineJob.java").read_text(encoding="utf-8")
        self.assertIn("UID_DEV_REVISIONS", source)
        self.assertIn("UID_DEV_METADATA", source)
        self.assertIn("uid(table,", source)
        self.assertIn("dev-pipeline-<id>_<transformation>", source)
        self.assertNotIn("allowNonRestoredState", source)


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


class PersistentCredentialTests(unittest.TestCase):
    def test_prepare_failure_detail_is_safe_and_keeps_gate_reason(self):
        self.assertEqual(
            event_prepare.safe_prepare_failure_detail(
                ValueError("persistent Dev credential mismatch")
            ),
            "persistent Dev credential mismatch",
        )
        self.assertEqual(
            event_prepare.safe_prepare_failure_detail(
                ValueError("password=super-secret-value")
            ),
            "ValueError",
        )

    def test_reuses_credentials_when_named_data_volumes_are_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime.env").write_text(
                "MYSQL_PASSWORD=" + "a" * 48 + "\n"
                "REDIS_PASSWORD=" + "b" * 48 + "\n",
                encoding="utf-8",
            )
            (root / "mysql.env").write_text(
                "MYSQL_ROOT_PASSWORD=" + "c" * 48 + "\n"
                "MYSQL_PASSWORD=" + "a" * 48 + "\n",
                encoding="utf-8",
            )
            (root / "redis.conf").write_text(
                "requirepass " + "b" * 48 + "\n",
                encoding="utf-8",
            )
            with patch.object(event_prepare, "ROOT", root):
                self.assertEqual(event_prepare._load_credentials(), ("a" * 48, "b" * 48, "c" * 48))

    def test_rejects_inconsistent_persistent_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime.env").write_text("MYSQL_PASSWORD=" + "a" * 48 + "\nREDIS_PASSWORD=" + "b" * 48 + "\n", encoding="utf-8")
            (root / "mysql.env").write_text("MYSQL_ROOT_PASSWORD=" + "c" * 48 + "\nMYSQL_PASSWORD=" + "d" * 48 + "\n", encoding="utf-8")
            (root / "redis.conf").write_text("requirepass " + "b" * 48 + "\n", encoding="utf-8")
            with patch.object(event_prepare, "ROOT", root), self.assertRaisesRegex(ValueError, "persistent Dev credential mismatch"):
                event_prepare._load_credentials()


class DatabaseIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebinds_retained_dev_database_to_new_run_id(self):
        class Cursor:
            def __init__(self):
                self.statements = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def execute(self, statement, params=()):
                self.statements.append((statement, params))

            async def fetchone(self):
                return ("development", "dev-previous-run", "Dev_EventPipeline")

        class Connection:
            def __init__(self, cursor):
                self.cursor_value = cursor

            def cursor(self):
                return self.cursor_value

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        class Pool:
            def __init__(self, connection):
                self.connection = connection

            def acquire(self):
                return self.connection

        cursor = Cursor()
        await event_runtime.ensure_database_identity(Pool(Connection(cursor)), settings())
        self.assertEqual(cursor.statements[0][0].split()[0:2], ["SELECT", "environment,run_id,database_name"])
        self.assertIn("UPDATE _pipeline_identity", cursor.statements[1][0])
        self.assertEqual(cursor.statements[1][1], ("dev-test-1", "development", "Dev_EventPipeline"))


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
            compose({name: "sha256:" + "a" * 64 for name in ("mysql", "redis", "worker", "flink")}),
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

    def test_existing_dev_project_allows_only_known_legacy_missing_label(self):
        base = {
            "Name": "/binhu-development-pipeline-backend-outbox-relay-1",
            "Config": {"Labels": {
                "com.docker.compose.project": "binhu-development-pipeline",
                "com.docker.compose.service": "backend-outbox-relay",
            }},
        }
        validate_existing_container_identity(base)
        current = copy.deepcopy(base)
        current["Config"]["Labels"]["binhu.environment"] = "development"
        validate_existing_container_identity(current)
        unknown = copy.deepcopy(base)
        unknown["Config"]["Labels"]["com.docker.compose.service"] = "relay"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            validate_existing_container_identity(unknown)
        foreign = copy.deepcopy(base)
        foreign["Config"]["Labels"]["binhu.environment"] = "production"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            validate_existing_container_identity(foreign)

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

    def test_flink_compose_gate_requires_dev_identity_and_checkpoint_volume(self):
        spec = flink_compose.specification(
            "sha256:" + "a" * 64,
            Path("/srv/binhu-environments/build-dev-pipeline-a9409fce"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flink-pipeline-compose.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            with patch.object(control, "FLINK_COMPOSE", path):
                control._validate_flink_compose()
                broken = copy.deepcopy(spec)
                broken["services"]["jobmanager"]["pids_limit"] = 128
                path.write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "pids_limit"):
                    control._validate_flink_compose()

    def test_apply_does_not_report_pending_when_flink_gate_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(
                json.dumps({"environment": "development", "project": control.PROJECT,
                            "run_id": "dev-20260915-gate"}),
                encoding="utf-8",
            )
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                return SimpleNamespace(returncode=0, stdout="started", stderr="")

            with patch.object(control, "ROOT", root), patch.object(control, "measure", return_value={}), \
                    patch.object(control.subprocess, "run", side_effect=run), \
                    patch.object(control, "_submit_and_verify_flink", side_effect=ValueError("identity mismatch")):
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    control.apply()
            self.assertTrue(any("up" in command for command in calls))

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
        taskmanager = spec["services"]["taskmanager"]
        self.assertEqual(taskmanager["environment"]["TASK_MANAGER_NUMBER_OF_TASK_SLOTS"], "3")
        self.assertIn("taskmanager.numberOfTaskSlots: 3", taskmanager["environment"]["FLINK_PROPERTIES"])
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

    def test_backend_relay_is_dev_only_and_uses_backend_targets(self):
        env = {"APP_ENVIRONMENT": "development", "DEV_RUN_ID": "dev-test-1",
               "BACKEND_MYSQL_HOST": "environment-mysql", "BACKEND_MYSQL_DATABASE": "Dev_OnlineData",
               "BACKEND_MYSQL_USER": "environment_app", "BACKEND_MYSQL_PASSWORD": "x" * 48,
               "BACKEND_REDIS_URL": "redis://:synthetic@redis:6379/0"}
        result = backend_relay_configuration(env)
        self.assertEqual(result["BACKEND_MYSQL_DATABASE"], "Dev_OnlineData")
        for key, value in (("APP_ENVIRONMENT", "production"),
                           ("BACKEND_MYSQL_HOST", "binhu-mysql"),
                           ("BACKEND_REDIS_URL", "redis://production:6379/0")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                backend_relay_configuration({**env, key: value})

    def test_compose_includes_isolated_backend_outbox_relay(self):
        spec = compose({name: "sha256:" + "a" * 64 for name in ("mysql", "redis", "worker", "flink")})
        service = spec["services"]["backend-outbox-relay"]
        self.assertEqual(service["networks"], ["backend"])
        self.assertEqual(service["env_file"], ["backend-relay.env"])
        self.assertEqual(service["command"][-1], "backend-outbox-relay")
        self.assertEqual(service["labels"]["binhu.environment"], "development")
        self.assertEqual(service["pids_limit"], 128)
        self.assertEqual(service["logging"]["options"], {"max-size": "5m", "max-file": "2"})

    def test_business_bridge_receives_pipeline_and_backend_runtime_credentials(self):
        spec = compose({name: "sha256:" + "a" * 64 for name in ("mysql", "redis", "worker", "flink")})
        service = spec["services"]["business-bridge"]
        self.assertEqual(service["env_file"], ["runtime.env", "backend-relay.env"])

    def test_control_expected_networks_match_backend_relay_compose(self):
        self.assertEqual(control.expected_networks("backend-outbox-relay"), {"binhu-development_internal"})
        self.assertEqual(control.expected_networks("business-bridge"), {"binhu-development-eventbus_internal", "binhu-development_internal"})
        self.assertEqual(control.expected_networks("relay"), {"binhu-development-eventbus_internal"})

    def test_compose_includes_isolated_python_metadata_worker(self):
        spec = compose({name: "sha256:" + "a" * 64 for name in ("mysql", "redis", "worker", "flink")})
        service = spec["services"]["python-metadata-worker"]
        self.assertEqual(service["networks"], ["internal"])
        self.assertEqual(service["command"][-1], "python-metadata-worker")
        self.assertEqual(service["labels"]["binhu.environment"], "development")
        self.assertEqual(service["pids_limit"], 128)
        self.assertEqual(service["logging"]["options"], {"max-size": "5m", "max-file": "2"})

    def test_compose_includes_read_only_resident_dual_track_monitor(self):
        spec = compose({name: "sha256:" + "a" * 64 for name in ("mysql", "redis", "worker", "flink")})
        service = spec["services"]["dual-track-monitor"]
        self.assertEqual(service["command"], ["python", "-m", "event_pipeline.runtime", "dual-track-monitor"])
        self.assertTrue(service["read_only"])
        self.assertIn("evidence:/var/lib/binhu-dev-event-pipeline/evidence", service["volumes"])
        self.assertIn("./runtime.py:/opt/dev-pipeline/event_pipeline/runtime.py:ro", service["volumes"])
        self.assertIn("./dual_track_monitor.py:/opt/dev-pipeline/event_pipeline/dual_track_monitor.py:ro", service["volumes"])
        self.assertEqual(service["mem_limit"], "128m")
        self.assertEqual(service["pids_limit"], 128)
        self.assertEqual(service["logging"]["options"], {"max-size": "5m", "max-file": "2"})

    def test_resident_monitor_report_is_redacted_and_pauses(self):
        row = {"task_id": "t_fullchain:1", "source_id": 1,
               "revision": 3, "event_count": 2, "changed_field_count": 2,
               "created_count": 1, "saved_count": 1, "claimed_count": 0,
               "assigned_count": 0, "reviewed_count": 0, "archived_count": 0,
               "deleted_count": 0, "备注": "must not be emitted"}
        report = build_report([row], [{**row, "revision": 4}], 2, 2,
                              "dev-test-1", "dual-track-test-1")
        self.assertFalse(report["passed"])
        self.assertEqual(report["unattributed_difference_count"], 1)
        with tempfile.TemporaryDirectory() as root:
            evidence = Path(root)
            write_cycle(evidence, report, 0)
            payload = (next(evidence.glob("comparison-*.json"))).read_text(encoding="utf-8")
            self.assertNotIn("must not be emitted", payload)
            self.assertTrue(next(evidence.glob("alert-*.json")).is_file())
            self.assertIn('"status": "paused"', (evidence / "status.json").read_text(encoding="utf-8"))

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
        spec = compose({name: "sha256:" + "a" * 64 for name in ("mysql", "redis", "worker", "flink")})
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
                         "dev.task.events.v1", "Dev_EventPipeline", "dev_task_metadata",
                         "event_count", "changed_field_count", "CARDINALITY(changed_fields)",
                         "dev_unique_events", "COUNT(DISTINCT event_id)"):
            self.assertIn(expected, sql)
        self.assertIn(
            "autoReconnect=true&maxReconnects=3&initialTimeout=2&tcpKeepAlive=true&connectTimeout=5000&socketTimeout=15000",
            sql,
        )
        self.assertNotIn("shadow", sql)
        with self.assertRaises(ValueError):
            render({**settings(), "MYSQL_PASSWORD": "x';secret"})


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_outbox_relay_publishes_bounded_metadata(self):
        class Cursor:
            async def execute(self, *args): self.args = args
            async def fetchall(self): return []
        class Conn:
            async def begin(self): pass
            async def commit(self): pass
            async def rollback(self): pass
            def cursor(self):
                class Ctx:
                    async def __aenter__(self): return Cursor()
                    async def __aexit__(self, *args): pass
                return Ctx()
        class Pool:
            def acquire(self):
                class Ctx:
                    async def __aenter__(self): return Conn()
                    async def __aexit__(self, *args): pass
                return Ctx()
        redis = AsyncMock()
        relay = BackendOutboxRelay(Pool(), redis, {"BACKEND_REDIS_STREAM_KEY": "binhu:events"})
        row = ("11111111-1111-4111-8111-111111111111", 1, "online", "online.task.changed",
               "online_task", "全链条:synthetic-row", 2, '["authenticated"]', "pending", 0,
               None, None, None, "", "", None, None)
        relay._claim = AsyncMock(return_value=[row])
        relay._finish = AsyncMock()
        self.assertEqual(await relay.run_once(), "published")
        payload = json.loads(redis.xadd.call_args.args[1]["event"])
        self.assertEqual(payload["aggregate_revision"], 2)
        self.assertNotIn("detail", payload)
        relay._claim.assert_awaited_once()

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
