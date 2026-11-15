import base64
import hashlib
import json
import pytest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from deploy.environments.event_pipeline.domain_pipeline import DomainPipelineOrchestrator
from deploy.environments.event_pipeline.flink_sql import render
from deploy.environments.event_pipeline.flink_submission import (
    parse_runtime_identity,
    validate_sql_identity,
)
from deploy.environments.event_pipeline import schema_registry
from deploy.environments.event_pipeline.business_bridge import PRODUCTION_TASK_CONSUMER_GROUP
from deploy.environments.event_pipeline.production_compose import (
    bridge_environment as production_bridge_environment,
    compose as production_compose,
    pipeline_sql as production_pipeline_sql,
    runtime_environment as production_runtime_environment,
)
from deploy.environments.event_pipeline.business_bridge import run_production
from deploy.environments.event_pipeline.runtime import (
    backend_relay_configuration,
    configuration,
)


RUN_ID = "PRD-rollout-20261114"
EVENT_KEY = "x" * 48
REDIS_URL = "redis://:synthetic-password@redis:6379/0"


def production_runtime():
    return {
        "APP_ENVIRONMENT": "production",
        "PRODUCTION_RUN_ID": RUN_ID,
        "KAFKA_BOOTSTRAP_SERVERS": "production-kafka:9092",
        "BACKEND_REDIS_URL": REDIS_URL,
        "BACKEND_REDIS_STREAM_KEY": "binhu:events",
        "TASK_EVENT_ENCRYPTION_KEY": EVENT_KEY,
    }


def test_production_runtime_fences_run_kafka_and_backend_redis_identity():
    runtime = production_runtime()

    assert configuration(runtime)["RUN_ID"] == RUN_ID
    assert configuration(runtime)["BACKEND_REDIS_STREAM_KEY"] == "binhu:events"
    assert parse_runtime_identity("\n".join(f"{key}={value}" for key, value in runtime.items())) == RUN_ID
    plan = DomainPipelineOrchestrator("production", RUN_ID).plan()
    assert set(plan.flink_sources) == {"dev_task_events_redis"}
    assert plan.kafka_topics == (
        "production.task.read-model.dlq.v4",
        "production.task.read-model.v4",
    )

    with pytest.raises(ValueError, match="Production Kafka"):
        configuration({**runtime, "KAFKA_BOOTSTRAP_SERVERS": "staging-kafka-1:9092"})
    with pytest.raises(ValueError, match="Production Kafka"):
        configuration({**runtime, "BACKEND_REDIS_URL": "redis://:synthetic-password@environment-redis:6379/0"})
    with pytest.raises(ValueError, match="Production Kafka"):
        configuration({**runtime, "BACKEND_REDIS_STREAM_KEY": "staging:events"})
    bridge_runtime = dict(runtime)
    bridge_runtime.pop("TASK_EVENT_ENCRYPTION_KEY")
    assert configuration(bridge_runtime, require_task_event_key=False)["RUN_ID"] == RUN_ID
    with pytest.raises(ValueError, match="Production Kafka"):
        configuration(bridge_runtime)


def test_production_does_not_start_a_second_backend_outbox_relay():
    env = {
        "APP_ENVIRONMENT": "production",
        "PRODUCTION_RUN_ID": RUN_ID,
        "BACKEND_MYSQL_HOST": "mysql",
        "BACKEND_MYSQL_DATABASE": "OnlineData",
        "BACKEND_MYSQL_USER": "binhu",
        "BACKEND_MYSQL_PASSWORD": "synthetic-database-password-0123456789",
        "BACKEND_REDIS_URL": REDIS_URL,
        "BACKEND_REDIS_STREAM_KEY": "binhu:events",
    }
    with pytest.raises(ValueError):
        backend_relay_configuration(env)


def test_production_schema_registry_contract_is_task_only_and_environment_scoped(monkeypatch):
    contracts = schema_registry._contracts("production")
    assert set(contracts) == {"instruction_audit"}
    assert contracts["instruction_audit"]["properties"]["environment"] == {"const": "production"}
    assert contracts["instruction_audit"]["properties"]["run_id"]["pattern"] == (
        "^PRD-[A-Za-z0-9_-]{1,48}$"
    )
    assert schema_registry.subject_for("production") == "production.task.read-model.v4-value"
    with pytest.raises(ValueError, match="unsupported schema subject"):
        schema_registry.request(
            "/subjects/production.online-summary.facts.v1-value/versions",
            subject="production.online-summary.facts.v1-value",
        )

    observed = []

    def fake_request(path, method="GET", body=None, *, subject=schema_registry.SUBJECT):
        observed.append(subject)
        expected = contracts["instruction_audit"]
        return {"schemaType": "JSON", "schema": __import__("json").dumps(expected),
                "id": 17, "version": 1}

    monkeypatch.setattr(schema_registry, "request", fake_request)
    result = schema_registry.verify("production")
    assert result["verified"] is True
    assert set(result["subjects"]) == {"task"}
    assert observed == ["production.task.read-model.v4-value"]


def test_production_bridge_uses_stable_consumer_group():
    assert PRODUCTION_TASK_CONSUMER_GROUP == "binhu-production-task-read-model-v4"


def test_production_compose_reuses_backend_redis_without_derived_datastores():
    images = {name: "sha256:" + str(index) * 64
              for index, name in enumerate(("kafka", "schema_registry", "flink", "worker"), 1)}
    spec = production_compose(images, RUN_ID)
    services = spec["services"]

    assert set(services) == {
        "production-kafka", "schema-registry", "production-bridge", "jobmanager", "taskmanager",
        "submit-production-flink-job",
        "provision-task-topic", "provision-dlq-topic", "provision-registry-topic",
        "register-production-schema",
    }
    assert "mysql" not in services and "redis" not in services
    assert spec["networks"]["backend"] == {"name": "binhu_default", "external": True}
    assert services["production-bridge"]["networks"] == ["internal", "backend"]
    assert services["production-bridge"]["env_file"] == ["bridge.env"]
    assert services["taskmanager"]["networks"] == ["internal", "backend"]
    assert services["taskmanager"]["env_file"] == ["runtime.env"]
    assert services["jobmanager"]["networks"] == ["internal"]
    submitter = services["submit-production-flink-job"]
    assert submitter["command"] == [
        "/opt/flink/bin/flink", "run", "-m", "jobmanager:8081",
        "-c", "PipelineJob", "/opt/flink/usrlib/pipeline-job.jar", RUN_ID,
    ]
    assert submitter["depends_on"]["jobmanager"]["condition"] == "service_healthy"
    assert "./artifacts/pipeline.sql:/opt/flink/private/pipeline.sql:ro" in submitter["volumes"]
    assert all(f"./artifacts/dependencies/{name}:/opt/flink/lib/{name}:ro" in submitter["volumes"]
               for name in ("flink-sql-connector-kafka-3.3.0-1.20.jar",
                            "flink-connector-jdbc-3.3.0-1.20.jar",
                            "mysql-connector-j-8.4.0.jar"))
    assert services["jobmanager"]["healthcheck"]["test"][-1] == (
        "http://127.0.0.1:8081/overview"
    )
    assert spec["x-binhu-contract"]["rendered_memory_mib"] <= 5 * 1024
    assert spec["x-binhu-contract"]["topics"] == [
        "production.task.read-model.v4", "production.task.read-model.dlq.v4",
    ]
    assert services["production-kafka"]["environment"]["KAFKA_DEFAULT_REPLICATION_FACTOR"] == "1"
    assert services["provision-task-topic"]["profiles"] == ["bootstrap"]
    assert services["register-production-schema"]["command"][-1] == "apply"

    with pytest.raises(ValueError, match="immutable"):
        production_compose({**images, "worker": "worker:latest"}, RUN_ID)


def test_production_runtime_environment_is_fixed_and_encodes_redis_password():
    env = production_runtime_environment(RUN_ID, "synthetic:@/secret", EVENT_KEY)
    assert env["APP_ENVIRONMENT"] == "production"
    assert env["PRODUCTION_RUN_ID"] == RUN_ID
    assert env["KAFKA_BOOTSTRAP_SERVERS"] == "production-kafka:9092"
    assert env["BACKEND_REDIS_STREAM_KEY"] == "binhu:events"
    assert "synthetic%3A%40%2Fsecret" in env["BACKEND_REDIS_URL"]
    assert env["TASK_EVENT_ENCRYPTION_KEY"] == EVENT_KEY
    bridge_env = production_bridge_environment(RUN_ID, "synthetic:@/secret")
    assert "TASK_EVENT_ENCRYPTION_KEY" not in bridge_env
    assert bridge_env["BACKEND_REDIS_URL"] == env["BACKEND_REDIS_URL"]
    with pytest.raises(ValueError, match="credentials"):
        production_runtime_environment(RUN_ID, "", EVENT_KEY)


def test_production_flink_sql_is_task_only_and_contains_no_database_or_secrets():
    sql = production_pipeline_sql(RUN_ID)
    identity = validate_sql_identity(sql, RUN_ID)
    assert identity["environment"] == "production"
    assert identity["topics"] == ["production.task.read-model.v4"]
    assert identity["sinks"] == ["task-read-model-redis"]
    assert "jdbc:mysql" not in sql
    assert "render-only" not in sql
    assert "PASSWORD" not in sql


def _production_bridge_source_event():
    event_id = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
    command_id = str(uuid.UUID("33333333-3333-4333-8333-333333333333"))
    operation_id = str(uuid.UUID("22222222-2222-4222-8222-222222222222"))
    ciphertext = b"opaque-ciphertext"
    aad = json.dumps({
        "command_id": command_id, "contract": "binhu.task-snapshot.v2",
        "environment": "production", "event_id": event_id,
        "revision": 7, "task_id": "t_fullchain:1",
    }, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 4, "command_id": command_id, "event_id": event_id,
        "event_type": "online.task.changed", "domain": "online",
        "aggregate_type": "online_task", "aggregate_id": "t_fullchain:1",
        "task_id": "t_fullchain:1", "source_id": 9, "operation_id": operation_id,
        "changed_fields": ["check_result"], "aggregate_revision": 7,
        "occurred_at": "2026-11-12T00:00:00Z",
        "encrypted_payload": {
            "algorithm": "AES-256-GCM", "key_version": "task-event-v2",
            "nonce": base64.urlsafe_b64encode(b"0123456789ab").decode().rstrip("="),
            "ciphertext": base64.urlsafe_b64encode(ciphertext).decode().rstrip("="),
            "aad": base64.urlsafe_b64encode(aad).decode().rstrip("="),
            "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        },
    }


@pytest.mark.asyncio
async def test_production_bridge_acks_only_after_kafka_and_never_deletes_shared_stream_entry():
    class StopAfterBatch(Exception):
        pass

    class FakeRedis:
        def __init__(self, event):
            self.event = event
            self.calls = []
            self.reads = 0

        async def ping(self):
            self.calls.append(("ping",))

        async def xgroup_create(self, stream, group, *, id, mkstream):
            self.calls.append(("group", stream, group, id, mkstream))

        async def xautoclaim(self, *args, **kwargs):
            return ["0-0", [], []]

        async def xreadgroup(self, group, consumer, streams, *, count, block):
            self.reads += 1
            if self.reads > 1:
                raise StopAfterBatch()
            return [("binhu:events", [("1720000000000-0", {
                "event": json.dumps(self.event), "event_id": self.event["event_id"],
            })])]

        async def xack(self, stream, group, entry_id):
            self.calls.append(("ack", stream, group, entry_id))

        async def xdel(self, *args):
            self.calls.append(("delete", *args))

        async def hset(self, key, *, mapping):
            self.calls.append(("state", key, mapping))

        async def lpush(self, *args):
            self.calls.append(("quarantine", *args))

        async def ltrim(self, *args):
            self.calls.append(("trim-quarantine", *args))

        async def aclose(self):
            self.calls.append(("close",))

    class FakeProducer:
        def __init__(self, *, fail_send=False, **kwargs):
            self.calls = []
            self.fail_send = fail_send

        async def start(self):
            self.calls.append(("start",))

        async def send_and_wait(self, topic, *, value, key):
            self.calls.append(("send", topic, value, key))
            if self.fail_send:
                raise TimeoutError("synthetic Kafka unavailable")

        async def stop(self):
            self.calls.append(("stop",))

    async def run_once(*, fail_send=False):
        redis_client = FakeRedis(_production_bridge_source_event())
        producer = FakeProducer(fail_send=fail_send)
        redis_module = SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *args, **kwargs: redis_client))
        aiokafka_module = SimpleNamespace(AIOKafkaProducer=lambda **kwargs: producer)
        with patch.dict("sys.modules", {
            "redis": SimpleNamespace(asyncio=redis_module),
            "redis.asyncio": redis_module,
            "aiokafka": aiokafka_module,
        }):
            with pytest.raises(StopAfterBatch) if not fail_send else pytest.raises(TimeoutError):
                await run_production({
                    "APP_ENVIRONMENT": "production", "RUN_ID": RUN_ID,
                    "KAFKA_BOOTSTRAP_SERVERS": "production-kafka:9092",
                    "BACKEND_REDIS_URL": REDIS_URL,
                    "BACKEND_REDIS_STREAM_KEY": "binhu:events",
                })
        return redis_client, producer

    redis_client, producer = await run_once()
    assert ("group", "binhu:events", PRODUCTION_TASK_CONSUMER_GROUP, "0-0", True) in redis_client.calls
    assert any(call[0] == "ack" for call in redis_client.calls)
    assert not any(call[0] == "delete" for call in redis_client.calls)
    assert any(call[0] == "send" and call[1] == "production.task.read-model.v4"
               for call in producer.calls)

    redis_client, _ = await run_once(fail_send=True)
    assert not any(call[0] == "ack" for call in redis_client.calls)


def test_production_flink_job_reads_task_topic_and_has_no_mysql_sink():
    runtime = production_runtime()

    sql = render(runtime)
    identity = validate_sql_identity(sql, RUN_ID)

    assert identity["environment"] == "production"
    assert identity["topics"] == ["production.task.read-model.v4"]
    assert identity["consumer_groups"] == [RUN_ID + "-flink-redis"]
    assert identity["sinks"] == ["task-read-model-redis"]
    assert "connector' = 'jdbc'" not in sql
    assert "MYSQL_PASSWORD" not in sql
    assert "staging." not in sql
