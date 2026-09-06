from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/kafka-shadow/protocol_smoke.py"


def load_protocol_smoke() -> Any:
    spec = importlib.util.spec_from_file_location("protocol_smoke_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_project(root: Path, *, name: str, run_id: str = "RUN-001") -> Path:
    project = root / name
    project.mkdir()
    (project / ".env").write_text(
        "\n".join(
            (
                "KAFKA_PROJECT=binhu-kafka-shadow-run-001",
                "KAFKA_NETWORK=binhu-kafka-shadow-run-001-network",
                f"KAFKA_RUN_ID={run_id}",
                "KAFKA_IMAGE=docker.1panel.live/apache/kafka@sha256:" + "a" * 64,
                "APICURIO_IMAGE=docker.1panel.live/apicurio/apicurio-registry-mem@sha256:" + "b" * 64,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    artifacts = project / "artifacts"
    artifacts.mkdir()
    (artifacts / "deployment-identity.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": "binhu-kafka-shadow-run-001",
                "network": "binhu-kafka-shadow-run-001-network",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return project


class ProtocolSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_protocol_smoke()

    def test_rejects_wrong_production_and_old_loadtest_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                ("production", "production target is forbidden"),
                ("old-loadtest", "old loadtest target is forbidden"),
                ("flink-poc-20260906", "old loadtest target is forbidden"),
            )
            for suffix, expected in cases:
                with self.subTest(suffix=suffix):
                    project = root / f"binhu-eventbus-shadow-{suffix}"
                    project.mkdir()
                    with self.assertRaisesRegex(ValueError, expected):
                        self.module.validate_project(project, "RUN-001")

    def test_run_id_must_match_isolated_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = write_project(Path(directory), name="binhu-eventbus-shadow-run-001")
            with self.assertRaisesRegex(ValueError, "KAFKA_RUN_ID"):
                self.module.validate_project(project, "RUN-002")

    def test_deployment_identity_must_match_all_env_scope_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = write_project(Path(directory), name="binhu-eventbus-shadow-run-001")
            identity = project / "artifacts" / "deployment-identity.json"
            identity.write_text(
                json.dumps(
                    {
                        "run_id": "RUN-001",
                        "project": "binhu-kafka-shadow-other-run",
                        "network": "binhu-kafka-shadow-run-001-network",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "deployment identity"):
                self.module.validate_project(project, "RUN-001")

    def test_attempt_uses_new_scoped_topic_and_artifact_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = write_project(Path(directory), name="binhu-eventbus-shadow-run-001")
            context = self.module.validate_project(project, "RUN-001", 2)
            self.assertEqual(context.topic, "binhu.shadow.protocol.run-001.a02")
            self.assertIn("attempt-02", context.result_path.name)
            self.assertIn("attempt-02", context.log_path.name)

    def test_failure_after_stop_restores_original_leader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = write_project(Path(directory), name="binhu-eventbus-shadow-run-001")
            context = self.module.validate_project(project, "RUN-001")
            calls: list[tuple[list[str], Path, float, str | None]] = []
            produced: str | None = None
            leader_stopped = False

            def result(
                args: list[str],
                *,
                stdout: str = "",
                stderr: str = "",
                returncode: int = 0,
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(args, returncode, stdout, stderr)

            def runner(
                args: list[str],
                *,
                cwd: Path,
                timeout: float,
                input_text: str | None = None,
            ) -> subprocess.CompletedProcess[str]:
                nonlocal leader_stopped, produced
                calls.append((args, cwd, timeout, input_text))
                self.assertEqual(cwd, project.resolve())
                self.assertGreater(timeout, 0)
                if "config" in args:
                    return result(args)
                if "ps" in args:
                    records = [
                        {
                            "Name": f"{context.project}-{service}",
                            "Service": service,
                            "State": "exited" if service == "kafka-1" and leader_stopped else "running",
                        }
                        for service in ("kafka-1", "kafka-2", "kafka-3", "schema-registry")
                    ]
                    return result(args, stdout=json.dumps(records))
                if args[:2] == ["docker", "inspect"]:
                    return result(args, stdout=json.dumps(self._inspect_records(context)))
                if args[:3] == ["docker", "network", "inspect"]:
                    return result(
                        args,
                        stdout=json.dumps(
                            [
                                {
                                    "Name": context.network,
                                    "Labels": {
                                        "com.docker.compose.project": context.project,
                                        "com.docker.compose.network": "internal",
                                    },
                                    "Containers": {
                                        str(index): {"Name": f"/{context.project}-{service}"}
                                        for index, service in enumerate(
                                            ("kafka-1", "kafka-2", "kafka-3", "schema-registry"),
                                            start=1,
                                        )
                                    },
                                    "Internal": True,
                                }
                            ]
                        ),
                        stderr="network diagnostics",
                    )
                if any(arg.endswith("kafka-console-producer.sh") for arg in args):
                    produced = input_text
                    return result(args)
                if any(arg.endswith("kafka-console-consumer.sh") for arg in args):
                    return result(args, stdout=produced or "")
                if any(arg.endswith("kafka-metadata-quorum.sh") for arg in args):
                    return result(
                        args,
                        stdout=(
                            "LeaderId: 1\n"
                            'CurrentVoters: [{"id": 1, "endpoints": [{"host": "kafka-1", "port": 9093}]}, '
                            '{"id": 2, "endpoints": [{"host": "kafka-2", "port": 9093}]}, '
                            '{"id": 3, "endpoints": [{"host": "kafka-3", "port": 9093}]}]\n'
                        ),
                    )
                if args[-2:] == ["stop", "kafka-1"]:
                    leader_stopped = True
                    return result(args)
                if args[-2:] == ["start", "kafka-1"]:
                    leader_stopped = False
                    return result(args)
                if any(arg.endswith("kafka-topics.sh") for arg in args):
                    return result(args, stdout=self._topic_description(context.topic))
                return result(args)

            def fail_sleep(seconds: float) -> None:
                self.assertEqual(seconds, self.module.LEADER_STOP_SECONDS)
                raise RuntimeError("simulated phase failure")

            with self.assertRaisesRegex(RuntimeError, "simulated phase failure"):
                self.module.run_smoke(
                    context,
                    runner=runner,
                    sleep_fn=fail_sleep,
                )

            stop_indexes = [
                index for index, (args, _cwd, _timeout, _input) in enumerate(calls)
                if args[-2:] == ["stop", "kafka-1"]
            ]
            start_indexes = [
                index for index, (args, _cwd, _timeout, _input) in enumerate(calls)
                if args[-2:] == ["start", "kafka-1"]
            ]
            self.assertEqual(len(stop_indexes), 1)
            self.assertEqual(len(start_indexes), 1)
            self.assertGreater(start_indexes[0], stop_indexes[0])
            artifact_text = context.log_path.read_text(encoding="utf-8")
            self.assertIn('"stdout":', artifact_text)
            self.assertIn('"stderr":', artifact_text)
            self.assertIn("network diagnostics", artifact_text)

    def test_success_path_replays_old_records_then_produces_a_new_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = write_project(Path(directory), name="binhu-eventbus-shadow-run-001")
            context = self.module.validate_project(project, "RUN-001", 2)
            calls: list[tuple[list[str], str | None]] = []
            stored: list[dict[str, Any]] = []
            leader_stopped = False

            def completed(
                args: list[str],
                *,
                stdout: str = "",
                stderr: str = "",
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(args, 0, stdout, stderr)

            def runner(
                args: list[str],
                *,
                cwd: Path,
                timeout: float,
                input_text: str | None = None,
            ) -> subprocess.CompletedProcess[str]:
                nonlocal leader_stopped
                self.assertEqual(cwd, project.resolve())
                self.assertGreater(timeout, 0)
                calls.append((args, input_text))
                if "config" in args:
                    return completed(args)
                if "ps" in args:
                    rows = [
                        {
                            "Name": f"{context.project}-{service}",
                            "Service": service,
                            "State": "exited" if service == "kafka-1" and leader_stopped else "running",
                        }
                        for service in ("kafka-1", "kafka-2", "kafka-3", "schema-registry")
                    ]
                    return completed(args, stdout=json.dumps(rows))
                if args[:2] == ["docker", "inspect"]:
                    return completed(args, stdout=json.dumps(self._inspect_records(context)))
                if args[:3] == ["docker", "network", "inspect"]:
                    return completed(
                        args,
                        stdout=json.dumps(
                            [
                                {
                                    "Name": context.network,
                                    "Labels": {
                                        "com.docker.compose.project": context.project,
                                        "com.docker.compose.network": "internal",
                                    },
                                    "Containers": {
                                        str(index): {"Name": f"/{context.project}-{service}"}
                                        for index, service in enumerate(
                                            ("kafka-1", "kafka-2", "kafka-3", "schema-registry"),
                                            start=1,
                                        )
                                    },
                                    "Internal": True,
                                }
                            ]
                        ),
                    )
                if any(arg.endswith("kafka-console-producer.sh") for arg in args):
                    self.assertIsNotNone(input_text)
                    stored.extend(json.loads(line) for line in (input_text or "").splitlines())
                    return completed(args)
                if any(arg.endswith("kafka-console-consumer.sh") for arg in args):
                    max_messages = int(args[args.index("--max-messages") + 1])
                    output = list(stored[:max_messages])
                    output.reverse()
                    return completed(args, stdout="\n".join(json.dumps(item) for item in output) + "\n")
                if any(arg.endswith("kafka-metadata-quorum.sh") for arg in args):
                    leader = 2 if leader_stopped else 1
                    return completed(
                        args,
                        stdout=(
                            f"LeaderId: {leader}\n"
                            'CurrentVoters: [{"id": 1, "endpoints": [{"host": "kafka-1", "port": 9093}]}, '
                            '{"id": 2, "endpoints": [{"host": "kafka-2", "port": 9093}]}, '
                            '{"id": 3, "endpoints": [{"host": "kafka-3", "port": 9093}]}]\n'
                        ),
                    )
                if args[-2:] == ["stop", "kafka-1"]:
                    leader_stopped = True
                    return completed(args)
                if args[-2:] == ["start", "kafka-1"]:
                    leader_stopped = False
                    return completed(args)
                if any(arg.endswith("kafka-topics.sh") for arg in args):
                    return completed(args, stdout=self._topic_description(context.topic))
                raise AssertionError(f"unexpected command: {args}")

            result = self.module.run_smoke(context, runner=runner, sleep_fn=lambda _seconds: None)
            self.assertEqual(result["status"], "passed")
            producer_inputs = [input_text for args, input_text in calls if any(
                arg.endswith("kafka-console-producer.sh") for arg in args
            )]
            self.assertEqual(len(producer_inputs), 2)
            self.assertNotEqual(producer_inputs[0], producer_inputs[1])
            self.assertTrue(context.result_path.is_file())
            self.assertTrue(context.log_path.is_file())

    @staticmethod
    def _inspect_records(context: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for service in ("kafka-1", "kafka-2", "kafka-3", "schema-registry"):
            labels = {
                "com.docker.compose.project": context.project,
                "com.docker.compose.service": service,
                "com.docker.compose.project.working_dir": str(context.project_dir),
                "binhu.shadow": "true",
                "binhu.shadow.run_id": context.run_id,
            }
            mounts: list[dict[str, str]] = []
            if service.startswith("kafka-"):
                mounts.extend(
                    (
                        {
                            "Type": "volume",
                            "Name": f"{context.project}_{service}-data",
                            "Destination": "/var/lib/kafka/data",
                        },
                    )
                )
            records.append(
                {
                    "Name": f"/{context.project}-{service}",
                    "Config": {"Labels": labels},
                    "Mounts": mounts,
                    "HostConfig": {
                        "Tmpfs": {
                            "/etc/kafka/secrets": "size=1m,mode=1777",
                            "/mnt/shared/config": "size=8m,mode=1777",
                        }
                    } if service.startswith("kafka-") else {"Tmpfs": {}},
                    "NetworkSettings": {
                        "Networks": {context.network: {}},
                        "Ports": {"9092/tcp": None} if service.startswith("kafka-") else {},
                    },
                }
            )
        return records

    @staticmethod
    def _topic_description(topic: str) -> str:
        return "\n".join(
            (
                f"Topic: {topic}\tPartitionCount: 3\tReplicationFactor: 2",
                f"Topic: {topic}\tPartition: 0\tLeader: 1\tReplicas: 1,2\tIsr: 1,2",
                f"Topic: {topic}\tPartition: 1\tLeader: 2\tReplicas: 2,3\tIsr: 2,3",
                f"Topic: {topic}\tPartition: 2\tLeader: 3\tReplicas: 3,1\tIsr: 3,1",
            )
        ) + "\n"


if __name__ == "__main__":
    unittest.main()
