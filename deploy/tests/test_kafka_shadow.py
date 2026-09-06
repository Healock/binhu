"""Static deployment boundaries; real Kafka acceptance is separately recorded."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_kraft_nodes_have_consistent_quorum_and_distinct_identity():
    config = yaml.safe_load((ROOT / "deploy/kafka-shadow/docker-compose.yml").read_text())
    brokers = [config["services"][f"kafka-{i}"] for i in range(1, 4)]
    assert [b["environment"]["KAFKA_NODE_ID"] for b in brokers] == [1, 2, 3]
    assert len({b["environment"]["CLUSTER_ID"] for b in brokers}) == 1
    assert len({b["environment"]["KAFKA_CONTROLLER_QUORUM_VOTERS"] for b in brokers}) == 1
    for i, broker in enumerate(brokers, start=1):
        env = broker["environment"]
        assert "KAFKA_CLUSTER_ID" not in env  # Apache image entrypoint uses CLUSTER_ID.
        assert env["KAFKA_ADVERTISED_LISTENERS"] == f"INTERNAL://kafka-{i}:9092"
        assert env["KAFKA_LOG_RETENTION_HOURS"] == 168
        assert env["KAFKA_DEFAULT_REPLICATION_FACTOR"] == 2
        assert env["KAFKA_MIN_INSYNC_REPLICAS"] == 2
        assert env["KAFKA_UNCLEAN_LEADER_ELECTION_ENABLE"] == "false"


def test_shadow_stack_cannot_publish_ports_or_claim_global_volumes():
    config = yaml.safe_load((ROOT / "deploy/kafka-shadow/docker-compose.yml").read_text())
    assert config["networks"]["internal"]["internal"] is True
    for service in config["services"].values():
        assert "ports" not in service
        assert "network_mode" not in service
        assert service["networks"] == ["internal"]
        assert service["mem_limit"] and service["cpus"] <= 1
        assert service["pids_limit"] == 256
        assert not service.get("privileged")
        if service.get("hostname", "").startswith("kafka-"):
            assert {m.split(":")[0] for m in service["tmpfs"]} == {
                "/etc/kafka/secrets", "/mnt/shared/config",
            }
        assert all(mount.split(":")[0] in config["volumes"] for mount in service.get("volumes", []))
    assert all(not volume for volume in config["volumes"].values())
