"""Closed Apicurio KafkaSQL configuration for the existing isolated Dev bus.

Only the registry service is replaced. Its journal lives in the Dev Kafka
cluster and survives container replacement; no shadow or production storage
is mounted. Callers must preserve the prior empty registry configuration.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

from .prepare import NETWORK, checked

PROJECT = "binhu-development-eventbus"
REGISTRY = PROJECT + "-schema-registry-1"
BROKER = PROJECT + "-kafka-1-1"
TOPIC = "dev.registry.storage.v1"
ROOT = Path("/srv/binhu-environments/development-eventbus")


def specification(image):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image or ""):
        raise ValueError("immutable KafkaSQL image required")
    return {"name": PROJECT, "services": {"schema-registry": {
        "image": image, "pull_policy": "never", "restart": "on-failure:3",
        "networks": ["internal"], "mem_limit": "768m", "cpus": .5,
        "pids_limit": 256, "security_opt": ["no-new-privileges:true"],
        "labels": {"binhu.environment": "development"},
        "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}},
        "environment": {
            "APP_ENVIRONMENT": "development",
            "REGISTRY_KAFKASQL_BOOTSTRAP_SERVERS": "kafka-1:9092,kafka-2:9092,kafka-3:9092",
            "REGISTRY_KAFKASQL_TOPIC": TOPIC,
            "REGISTRY_KAFKASQL_TOPIC_AUTO_CREATE": "false",
            "QUARKUS_HTTP_PORT": "8080",
            "JAVA_OPTIONS": "-Xms128m -Xmx384m -XX:ActiveProcessorCount=2 -XX:MaxMetaspaceSize=192m -XX:+ExitOnOutOfMemoryError",
        }}}, "networks": {"internal": {"external": True, "name": NETWORK}}}


def validate_network(network, registry, broker):
    if not network.get("Internal") or network.get("Labels", {}).get("com.docker.compose.project") != PROJECT:
        raise ValueError("Dev eventbus network required")
    if any(not x.get("Name", "").startswith("binhu-development-") for x in network.get("Containers", {}).values()):
        raise ValueError("foreign network member")
    for item, name, service in ((registry, REGISTRY, "schema-registry"), (broker, BROKER, "kafka-1")):
        labels = item["Config"].get("Labels", {})
        if (item["Name"].lstrip("/") != name or labels.get("com.docker.compose.project") != PROJECT
                or labels.get("com.docker.compose.service") != service
                or set(item["NetworkSettings"]["Networks"]) != {NETWORK}
                or not item["State"]["Running"]):
            raise ValueError("Dev service identity mismatch")


def prepare(image):
    if ROOT.resolve() != ROOT or ROOT.is_symlink() or ROOT.parent.is_symlink():
        raise ValueError("fixed Dev path required")
    target = ROOT / "registry-kafkasql-compose.json"
    if target.exists() or target.is_symlink():
        raise ValueError("existing prepared configuration must not be overwritten")
    network = json.loads(checked(["docker", "network", "inspect", NETWORK]))[0]
    registry, broker = json.loads(checked(["docker", "inspect", REGISTRY, BROKER]))
    validate_network(network, registry, broker)
    if checked(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip() != image:
        raise ValueError("image identity mismatch")
    with target.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(specification(image), indent=2))
    target.chmod(0o600)
    checked(["docker", "compose", "-f", str(target), "config", "--quiet"])
    return {"environment": "development", "project": PROJECT, "prepared": True,
            "applied": False, "journal_topic": TOPIC}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.image)))
    except Exception:
        raise SystemExit("Dev registry preparation refused; preserve current resources") from None
