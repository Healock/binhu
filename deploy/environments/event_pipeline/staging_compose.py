"""Closed Docker Compose model for one isolated Staging event-pipeline run.

The model is intentionally pure: it renders a definition but never talks to
Docker.  The fixed Staging gateway performs prepare/measure/apply after it has
verified the candidate manifest and the server-side environment boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .identity import validate_identity


IMAGE_KEYS = frozenset({"kafka", "schema_registry", "mysql", "redis", "worker", "flink"})
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BROKERS = ("staging-kafka-1", "staging-kafka-2", "staging-kafka-3")
EVENT_TOPIC = "staging.task.events.v1"
DLQ_TOPIC = "staging.task.events.dlq.v1"
REGISTRY_TOPIC = "staging.registry.storage.v1"
BACKEND_NETWORK = "binhu-staging_internal"
LOGGING = {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}


def project_for(run_id: str) -> str:
    validate_identity("staging", run_id)
    return "binhu-staging-event-pipeline-" + run_id.lower()


def network_for(run_id: str) -> str:
    return project_for(run_id) + "_internal"


def cluster_id_for(run_id: str) -> str:
    """Return a deterministic, non-secret 22-character KRaft cluster ID."""
    validate_identity("staging", run_id)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    digest = hashlib.sha256(("binhu-staging-kraft:" + run_id).encode()).digest()
    value = int.from_bytes(digest, "big")
    chars = []
    for _ in range(22):
        chars.append(alphabet[value & 63])
        value >>= 6
    return "".join(chars)


def _images(images: dict[str, str]) -> dict[str, str]:
    if set(images) != IMAGE_KEYS or any(not DIGEST_RE.fullmatch(str(value or "")) for value in images.values()):
        raise ValueError("six immutable Staging event-pipeline image identities required")
    return dict(images)


def _labels(run_id: str) -> dict[str, str]:
    return {
        "binhu.environment": "staging",
        "binhu.run_id": run_id,
        "binhu.production_data": "false",
    }


def _common(run_id: str, *, pids: int = 128) -> dict[str, Any]:
    return {
        "labels": _labels(run_id),
        "pull_policy": "never",
        "restart": "on-failure:3",
        "pids_limit": pids,
        "logging": LOGGING,
        "security_opt": ["no-new-privileges:true"],
    }


def _broker_environment(index: int, run_id: str) -> dict[str, str]:
    voters = ",".join(f"{node}@{name}:9093" for node, name in enumerate(BROKERS, start=1))
    return {
        "CLUSTER_ID": cluster_id_for(run_id),
        "KAFKA_NODE_ID": str(index),
        "KAFKA_PROCESS_ROLES": "broker,controller",
        "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
        "KAFKA_ADVERTISED_LISTENERS": f"PLAINTEXT://{BROKERS[index - 1]}:9092",
        "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
        "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
        "KAFKA_INTER_BROKER_LISTENER_NAME": "PLAINTEXT",
        "KAFKA_CONTROLLER_QUORUM_VOTERS": voters,
        "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "3",
        "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "3",
        "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "2",
        "KAFKA_DEFAULT_REPLICATION_FACTOR": "3",
        "KAFKA_MIN_INSYNC_REPLICAS": "2",
        "KAFKA_NUM_PARTITIONS": "3",
        "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
        "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
        "KAFKA_LOG_RETENTION_HOURS": "24",
    }


def compose(images: dict[str, str], run_id: str) -> dict[str, Any]:
    """Render the complete isolated Staging event-pipeline topology."""
    images = _images(images)
    project = project_for(run_id)
    internal = network_for(run_id)
    services: dict[str, Any] = {}

    for index, broker in enumerate(BROKERS, start=1):
        services[broker] = {
            **_common(run_id, pids=256),
            "image": images["kafka"],
            "hostname": broker,
            "networks": ["internal"],
            "environment": _broker_environment(index, run_id),
            "mem_limit": "512m",
            "memswap_limit": "640m",
            "cpus": 0.5,
            "volumes": [
                f"{broker}-data:/var/lib/kafka/data",
                f"{broker}-secrets-tmpfs:/etc/kafka/secrets",
                f"{broker}-config-tmpfs:/mnt/shared/config",
            ],
            "healthcheck": {
                "test": ["CMD", "/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server", "127.0.0.1:9092", "--list"],
                "interval": "10s", "timeout": "8s", "retries": 18, "start_period": "60s",
            },
        }

    services["schema-registry"] = {
        **_common(run_id, pids=256),
        "image": images["schema_registry"],
        "networks": ["internal"],
        "mem_limit": "384m",
        "memswap_limit": "512m",
        "cpus": 0.5,
        "environment": {
            "APP_ENVIRONMENT": "staging",
            "REGISTRY_KAFKASQL_BOOTSTRAP_SERVERS": ",".join(f"{name}:9092" for name in BROKERS),
            "REGISTRY_KAFKASQL_TOPIC": REGISTRY_TOPIC,
            "REGISTRY_KAFKASQL_TOPIC_AUTO_CREATE": "false",
            "QUARKUS_HTTP_PORT": "8080",
            "JAVA_OPTIONS": "-Xms128m -Xmx256m -XX:ActiveProcessorCount=2 -XX:MaxMetaspaceSize=96m -XX:+ExitOnOutOfMemoryError",
        },
        "depends_on": {name: {"condition": "service_healthy"} for name in BROKERS},
    }

    services["staging-derived-mysql"] = {
        **_common(run_id, pids=256),
        "image": images["mysql"],
        "networks": ["internal"],
        "env_file": ["mysql.env"],
        "mem_limit": "768m",
        "memswap_limit": "1024m",
        "cpus": 0.75,
        "command": [
            "--innodb-buffer-pool-size=192M", "--max-connections=40",
            "--innodb-file-per-table=ON", "--innodb-redo-log-capacity=128M", "--skip-log-bin",
        ],
        "volumes": ["derived-mysql:/var/lib/mysql", "./init.sql:/docker-entrypoint-initdb.d/01-pipeline.sql:ro"],
        "healthcheck": {
            "test": ["CMD", "mysqladmin", "ping", "-h127.0.0.1", "--silent"],
            "interval": "5s", "timeout": "3s", "retries": 36, "start_period": "180s",
        },
    }
    services["staging-derived-redis"] = {
        **_common(run_id),
        "image": images["redis"],
        "networks": ["internal"],
        "mem_limit": "256m",
        "memswap_limit": "384m",
        "cpus": 0.25,
        "command": ["redis-server", "/usr/local/etc/redis/redis.conf"],
        "volumes": ["derived-redis:/data", "./redis.conf:/usr/local/etc/redis/redis.conf:ro"],
    }

    code_mount = "./code:/opt/dev-pipeline/event_pipeline:ro"
    worker_common = {
        **_common(run_id),
        "image": images["worker"],
        "env_file": ["runtime.env"],
        "read_only": True,
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"],
        "volumes": [code_mount],
        "networks": ["internal"],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "depends_on": {
            "staging-derived-mysql": {"condition": "service_healthy"},
            "staging-derived-redis": {"condition": "service_started"},
            "schema-registry": {"condition": "service_started"},
        },
    }
    for mode in ("relay", "bridge"):
        services[mode] = {
            **worker_common,
            "mem_limit": "256m" if mode == "relay" else "160m",
            "cpus": 0.75 if mode == "relay" else 0.25,
            "command": ["python", "-m", "event_pipeline.runtime", mode],
        }
    services["business-bridge"] = {
        **worker_common,
        "networks": ["internal", "backend"],
        "env_file": ["runtime.env", "backend-relay.env"],
        "mem_limit": "160m", "cpus": 0.25,
        "command": ["python", "-m", "event_pipeline.runtime", "business-bridge"],
    }
    services["backend-outbox-relay"] = {
        **_common(run_id),
        "image": images["worker"],
        "networks": ["backend"],
        "env_file": ["backend-relay.env"],
        "mem_limit": "160m", "memswap_limit": "224m", "cpus": 0.25,
        "read_only": True,
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"],
        "volumes": [code_mount],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "command": ["python", "-m", "event_pipeline.runtime", "backend-outbox-relay"],
    }
    services["python-metadata-worker"] = {
        **worker_common,
        "mem_limit": "256m", "memswap_limit": "320m", "cpus": 0.5,
        "command": ["python", "-m", "event_pipeline.runtime", "python-metadata-worker"],
    }

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
        "taskmanager.numberOfTaskSlots: 3",
        "parallelism.default: 1",
    ])
    flink_common = {
        **_common(run_id, pids=256),
        "image": images["flink"],
        "networks": ["internal"],
        "env_file": ["runtime.env"],
        "environment": {"APP_ENVIRONMENT": "staging", "STAGING_RUN_ID": run_id,
                        "FLINK_PROPERTIES": flink_properties},
        "volumes": [
            "flink-checkpoints:/opt/flink/checkpoints",
            "./pipeline.sql:/opt/flink/private/pipeline.sql:ro",
            "./pipeline-job.jar:/opt/flink/private/pipeline-job.jar:ro",
        ],
    }
    services["jobmanager"] = {
        **flink_common, "command": ["jobmanager"], "mem_limit": "768m",
        "memswap_limit": "896m", "cpus": 0.5,
    }
    services["taskmanager"] = {
        **flink_common, "command": ["taskmanager"], "mem_limit": "2048m",
        "memswap_limit": "2304m", "cpus": 1.5,
        "depends_on": {"jobmanager": {"condition": "service_started"}},
    }

    volumes: dict[str, Any] = {
        "derived-mysql": {"labels": _labels(run_id)},
        "derived-redis": {"labels": _labels(run_id)},
        "flink-checkpoints": {"labels": _labels(run_id)},
    }
    for broker in BROKERS:
        volumes[f"{broker}-data"] = {"labels": _labels(run_id)}
        for suffix, size in (("secrets-tmpfs", 1048576), ("config-tmpfs", 4194304)):
            volumes[f"{broker}-{suffix}"] = {
                "driver": "local",
                "driver_opts": {
                    "type": "tmpfs", "device": "tmpfs",
                    "o": f"uid=1000,gid=1000,mode=0750,size={size}",
                },
                "labels": _labels(run_id),
            }

    return {
        "name": project,
        "services": services,
        "networks": {
            "internal": {"name": internal, "internal": True, "labels": _labels(run_id)},
            "backend": {"external": True, "name": BACKEND_NETWORK},
        },
        "volumes": volumes,
    }


def model_sha256(images: dict[str, str], run_id: str) -> str:
    payload = json.dumps(compose(images, run_id), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "BACKEND_NETWORK", "BROKERS", "DLQ_TOPIC", "EVENT_TOPIC", "IMAGE_KEYS",
    "REGISTRY_TOPIC", "cluster_id_for", "compose", "model_sha256", "network_for", "project_for",
]
