"""Render the fixed, resource-bounded Production task read-model topology.

The definition is pure and does not call Docker. It reuses the Production
Backend Redis and existing Backend outbox relay; it has no derived database,
derived Redis, or competing MySQL relay.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .domain_pipeline import DomainPipelineOrchestrator
from .identity import validate_identity


PROJECT = "binhu-production-event-pipeline"
INTERNAL_NETWORK = PROJECT + "_internal"
BACKEND_NETWORK = "binhu_default"
REGISTRY_TOPIC = "production.registry.storage.v1"
DEPENDENCIES = (
    "flink-sql-connector-kafka-3.3.0-1.20.jar",
    "flink-connector-jdbc-3.3.0-1.20.jar",
    "mysql-connector-j-8.4.0.jar",
)
IMAGE_KEYS = frozenset({"kafka", "schema_registry", "flink", "worker"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOGGING = {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}
MAX_MEMORY_MIB = 5 * 1024


def _images(images: dict[str, str]) -> dict[str, str]:
    if (not isinstance(images, dict) or set(images) != IMAGE_KEYS
            or any(not _DIGEST.fullmatch(str(value or "")) for value in images.values())):
        raise ValueError("four immutable Production event-pipeline image identities required")
    return dict(images)


def _common(run_id: str, *, pids: int = 128) -> dict[str, Any]:
    return {
        "labels": {
            "binhu.environment": "production", "binhu.run_id": run_id,
            "binhu.production_data": "true",
        },
        "pull_policy": "never",
        "restart": "on-failure:3",
        "pids_limit": pids,
        "logging": _LOGGING,
        "security_opt": ["no-new-privileges:true"],
    }


def runtime_environment(run_id: str, redis_password: str, event_key: str) -> dict[str, str]:
    """Build the private runtime values from already provisioned credentials."""
    validate_identity("production", run_id)
    if (not redis_password or any(char in redis_password for char in "\r\n")
            or not event_key or len(event_key.encode("utf-8")) < 32
            or any(char in event_key for char in "\r\n")):
        raise ValueError("Production pipeline credentials are incomplete")
    return {
        "APP_ENVIRONMENT": "production",
        "PRODUCTION_RUN_ID": run_id,
        "KAFKA_BOOTSTRAP_SERVERS": "production-kafka:9092",
        "BACKEND_REDIS_URL": "redis://:" + quote(redis_password, safe="") + "@redis:6379/0",
        "BACKEND_REDIS_STREAM_KEY": "binhu:events",
        "TASK_EVENT_ENCRYPTION_KEY": event_key,
    }


def bridge_environment(run_id: str, redis_password: str) -> dict[str, str]:
    """Give the transport-only bridge no task decryption key."""
    validate_identity("production", run_id)
    if not redis_password or any(char in redis_password for char in "\r\n"):
        raise ValueError("Production Redis credential is incomplete")
    return {
        "APP_ENVIRONMENT": "production",
        "PRODUCTION_RUN_ID": run_id,
        "KAFKA_BOOTSTRAP_SERVERS": "production-kafka:9092",
        "BACKEND_REDIS_URL": "redis://:" + quote(redis_password, safe="") + "@redis:6379/0",
        "BACKEND_REDIS_STREAM_KEY": "binhu:events",
    }


def pipeline_sql(run_id: str) -> str:
    """Render the Production JobGraph SQL without embedding real credentials."""
    validate_identity("production", run_id)
    from .flink_sql import render

    return render({
        "APP_ENVIRONMENT": "production",
        "PRODUCTION_RUN_ID": run_id,
        "KAFKA_BOOTSTRAP_SERVERS": "production-kafka:9092",
        "BACKEND_REDIS_URL": "redis://:render-only@redis:6379/0",
        "TASK_EVENT_ENCRYPTION_KEY": "render-only-event-key-value-1234567890",
    })


def compose(images: dict[str, str], run_id: str) -> dict[str, Any]:
    """Return a fixed production topology with a 5 GiB total memory ceiling."""
    images = _images(images)
    validate_identity("production", run_id)
    plan = DomainPipelineOrchestrator("production", run_id).plan()
    event_topic = "production.task.read-model.v4"
    dlq_topic = "production.task.read-model.dlq.v4"
    if set(plan.kafka_topics) != {event_topic, dlq_topic}:
        raise ValueError("Production task topic registry mismatch")

    common = _common(run_id)
    flink_properties = "\n".join([
        "jobmanager.rpc.address: jobmanager",
        "jobmanager.memory.process.size: 640m",
        "jobmanager.memory.jvm-metaspace.size: 128m",
        "jobmanager.memory.jvm-overhead.min: 64m",
        "jobmanager.memory.jvm-overhead.max: 64m",
        "taskmanager.memory.process.size: 1792m",
        "taskmanager.memory.jvm-metaspace.size: 128m",
        "taskmanager.memory.jvm-overhead.min: 64m",
        "taskmanager.memory.jvm-overhead.max: 64m",
        "taskmanager.memory.network.min: 32m",
        "taskmanager.memory.network.max: 32m",
        "taskmanager.memory.managed.size: 64m",
        "taskmanager.numberOfTaskSlots: 1",
        "parallelism.default: 1",
    ])
    services: dict[str, Any] = {
        "production-kafka": {
            **_common(run_id, pids=256),
            "image": images["kafka"],
            "hostname": "production-kafka",
            "networks": ["internal"],
            "mem_limit": "1024m", "memswap_limit": "1280m", "cpus": 0.75,
            "environment": {
                "CLUSTER_ID": "MkU3OEVBNTcwNTJENDM2Qk",
                "KAFKA_NODE_ID": "1",
                "KAFKA_PROCESS_ROLES": "broker,controller",
                "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
                "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://production-kafka:9092",
                "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
                "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
                "KAFKA_INTER_BROKER_LISTENER_NAME": "PLAINTEXT",
                "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@production-kafka:9093",
                "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
                "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
                "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
                "KAFKA_DEFAULT_REPLICATION_FACTOR": "1",
                "KAFKA_MIN_INSYNC_REPLICAS": "1",
                "KAFKA_NUM_PARTITIONS": "3",
                "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
                "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
                "KAFKA_LOG_RETENTION_HOURS": "168",
                "KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m",
            },
            "volumes": ["kafka-data:/var/lib/kafka/data"],
            "healthcheck": {
                "test": ["CMD", "/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server",
                         "127.0.0.1:9092", "--list"],
                "interval": "10s", "timeout": "15s", "retries": 30,
                "start_period": "120s",
            },
        },
        "schema-registry": {
            **_common(run_id, pids=256),
            "image": images["schema_registry"], "networks": ["internal"],
            "mem_limit": "384m", "memswap_limit": "512m", "cpus": 0.5,
            "environment": {
                "APP_ENVIRONMENT": "production",
                "REGISTRY_KAFKASQL_BOOTSTRAP_SERVERS": "production-kafka:9092",
                "REGISTRY_KAFKASQL_TOPIC": REGISTRY_TOPIC,
                "REGISTRY_KAFKASQL_TOPIC_AUTO_CREATE": "false",
                "QUARKUS_HTTP_PORT": "8080",
                "JAVA_OPTIONS": "-Xms128m -Xmx256m -XX:ActiveProcessorCount=2 -XX:MaxMetaspaceSize=96m -XX:+ExitOnOutOfMemoryError",
            },
            "depends_on": {"production-kafka": {"condition": "service_healthy"}},
        },
        "production-bridge": {
            **common,
            "image": images["worker"],
            "networks": ["internal", "backend"],
            "env_file": ["bridge.env"],
            "mem_limit": "192m", "memswap_limit": "256m", "cpus": 0.25,
            "read_only": True,
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"],
            "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
            "command": ["python", "-m", "event_pipeline.runtime", "production-bridge"],
            "depends_on": {"production-kafka": {"condition": "service_healthy"}},
        },
    }

    for service_name, topic in (
        ("provision-task-topic", event_topic),
        ("provision-dlq-topic", dlq_topic),
        ("provision-registry-topic", REGISTRY_TOPIC),
    ):
        services[service_name] = {
            **_common(run_id),
            "image": images["kafka"], "profiles": ["bootstrap"],
            "networks": ["internal"], "mem_limit": "64m",
            "memswap_limit": "96m", "cpus": 0.1,
            "read_only": True,
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=8m"],
            "command": [
                "/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server",
                "production-kafka:9092", "--create", "--if-not-exists",
                "--topic", topic, "--partitions", "3", "--replication-factor", "1",
                "--config", "retention.ms=604800000", "--config", "min.insync.replicas=1",
            ],
            "depends_on": {"production-kafka": {"condition": "service_healthy"}},
        }
    services["register-production-schema"] = {
        **_common(run_id),
        "image": images["worker"], "profiles": ["bootstrap"],
        "networks": ["internal"],
        "mem_limit": "128m", "memswap_limit": "192m", "cpus": 0.2,
        "read_only": True,
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=8m"],
        "environment": {
            "APP_ENVIRONMENT": "production", "PRODUCTION_RUN_ID": run_id,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "command": ["python", "-m", "event_pipeline.schema_registry", "apply"],
        "depends_on": {"schema-registry": {"condition": "service_started"}},
    }

    flink_common = {
        **_common(run_id, pids=256),
        "image": images["flink"], "networks": ["internal"],
        "environment": {
            "APP_ENVIRONMENT": "production", "PRODUCTION_RUN_ID": run_id,
            "FLINK_PROPERTIES": flink_properties,
        },
        "volumes": [
            "flink-checkpoints:/opt/flink/checkpoints",
            "./artifacts/pipeline.sql:/opt/flink/private/pipeline.sql:ro",
            "./artifacts/pipeline-job.jar:/opt/flink/usrlib/pipeline-job.jar:ro",
            *[f"./artifacts/dependencies/{name}:/opt/flink/lib/{name}:ro" for name in DEPENDENCIES],
        ],
        "depends_on": {"production-kafka": {"condition": "service_healthy"}},
    }
    services["jobmanager"] = {
        **flink_common, "command": ["jobmanager"], "mem_limit": "768m",
        "memswap_limit": "896m", "cpus": 0.5,
        "healthcheck": {
            "test": ["CMD", "curl", "-fsS", "http://127.0.0.1:8081/overview"],
            "interval": "5s", "timeout": "3s", "retries": 36,
            "start_period": "30s",
        },
    }
    services["taskmanager"] = {
        **flink_common, "command": ["taskmanager"],
        "env_file": ["runtime.env"],
        "networks": ["internal", "backend"],
        "mem_limit": "2048m", "memswap_limit": "2304m", "cpus": 1.5,
        "depends_on": {
            "production-kafka": {"condition": "service_healthy"},
            "jobmanager": {"condition": "service_started"},
        },
    }
    services["submit-production-flink-job"] = {
        **_common(run_id),
        "image": images["flink"], "networks": ["internal"],
        "environment": {
            "APP_ENVIRONMENT": "production", "PRODUCTION_RUN_ID": run_id,
        },
        "volumes": [
            "./artifacts/pipeline.sql:/opt/flink/private/pipeline.sql:ro",
            "./artifacts/pipeline-job.jar:/opt/flink/usrlib/pipeline-job.jar:ro",
            *[f"./artifacts/dependencies/{name}:/opt/flink/lib/{name}:ro"
              for name in DEPENDENCIES],
        ],
        "mem_limit": "256m", "memswap_limit": "320m", "cpus": 0.25,
        "read_only": True,
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=8m"],
        "command": [
            "/opt/flink/bin/flink", "run", "-m", "jobmanager:8081",
            "-c", "PipelineJob", "/opt/flink/usrlib/pipeline-job.jar", run_id,
        ],
        "depends_on": {
            "jobmanager": {"condition": "service_healthy"},
            "taskmanager": {"condition": "service_started"},
        },
    }

    memory_mib = sum(int(service["mem_limit"].removesuffix("m"))
                     for service in services.values())
    if memory_mib > MAX_MEMORY_MIB:
        raise ValueError("Production event-pipeline memory budget exceeded")
    return {
        "name": PROJECT,
        "services": services,
        "networks": {
            "internal": {"name": INTERNAL_NETWORK, "internal": True},
            "backend": {"name": BACKEND_NETWORK, "external": True},
        },
        "volumes": {"kafka-data": {}, "flink-checkpoints": {}},
        "x-binhu-contract": {
            "environment": "production", "run_id": run_id,
            "memory_limit_mib": MAX_MEMORY_MIB,
            "rendered_memory_mib": memory_mib,
            "topics": [event_topic, dlq_topic],
            "outbox_relay": "existing-binhu-event-relay",
            "backend_redis": "existing-binhu-redis",
            "derived_mysql": False, "derived_redis": False,
        },
    }
