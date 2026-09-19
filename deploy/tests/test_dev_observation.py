import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.environments.event_pipeline import observation_control as observation


RUN_ID = "dev-20260919-dualtrack-monitor40"
OBSERVATION_ID = "obs-20260920-monitor40-6h"


def sample(sequence: int, *, memory_growth: int = 0, lag: int = 0, differences: int = 0):
    names = [*observation.PIPELINE_CONTAINERS.values(), observation.KAFKA_CONTAINER,
             observation.FLINK_CONTAINER]
    containers = {
        name: {
            "status": "running", "health": "healthy", "restart_count": 0,
            "oom_killed": False, "memory_limit": 256 * 1024**2,
            "log_max_size": "5m", "log_max_file": "2", "log_bytes": 1024,
        }
        for name in names
    }
    base = 40 * 1024**2 + sequence * memory_growth
    return {
        "memory": {name: base for name in observation.OBSERVED_MEMORY},
        "containers": containers,
        "monitor": {"unattributed_difference_count": differences},
        "redis": {"used_memory": 64 * 1024**2 + sequence * memory_growth,
                  "maxmemory": 192 * 1024**2, "oom_error_count": 0, "evicted_keys": 0},
        "mysql": {"Threads_connected": 10, "Threads_running": 1,
                  "Max_used_connections": 12, "Innodb_row_lock_current_waits": 0,
                  "Innodb_deadlocks": 2, "max_connections": 20},
        "kafka": {"lag": lag},
        "flink": {"checkpoint_completed": 100 + sequence,
                  "checkpoint_failed": 0, "checkpoint_in_progress": 0},
        "disk": {"used": 100 * 1024**3 + sequence * 1024**2,
                 "used_percent": 40.0},
    }


class DevObservationTests(unittest.TestCase):
    def test_fixed_six_hour_contract_has_thirteen_half_hour_samples(self):
        self.assertEqual(observation.OBSERVATION_SECONDS, 21600)
        self.assertEqual(observation.SAMPLE_SECONDS, 1800)
        self.assertEqual(observation.SAMPLE_COUNT, 13)
        observation.validate_identity(RUN_ID, OBSERVATION_ID)
        for run_id, observation_id in (("production", OBSERVATION_ID),
                                       (RUN_ID, "obs-bad"),
                                       (RUN_ID, "obs-20260920-a;rm")):
            with self.assertRaises(observation.ObservationError):
                observation.validate_identity(run_id, observation_id)

    def test_memory_parser_accepts_docker_binary_units(self):
        self.assertEqual(observation.memory_bytes("72.5MiB"), int(72.5 * 1024**2))
        self.assertEqual(observation.memory_bytes("1GiB"), 1024**3)
        with self.assertRaises(observation.ObservationError):
            observation.memory_bytes("unknown")

    def test_kafka_lag_parser_uses_the_fixed_broker_command(self):
        output = """GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG CONSUMER-ID HOST CLIENT-ID
dev-20260919-dualtrack-monitor40-flink dev.task.events.v1 0 100 102 2 - - -
dev-20260919-dualtrack-monitor40-flink dev.task.events.v1 1 200 200 0 - - -
"""
        with patch.object(observation, "checked", return_value=output) as runner:
            result = observation._kafka_lag(RUN_ID)
        self.assertEqual(result["partitions"], 2)
        self.assertEqual(result["lag"], 2)
        command = runner.call_args.args[0]
        self.assertIn(observation.KAFKA_GROUPS, command)

    def test_healthy_six_hour_samples_pass(self):
        samples = [sample(index) for index in range(observation.SAMPLE_COUNT)]
        manual = {"tasks": {"metadata_reconciliation": "passed",
                            "schema_contract_reconciliation": "passed",
                            "status_summary": "passed",
                            "daily_report_refresh": "not_applicable_domain_not_migrated",
                            "cleanup_task": "not_applicable_no_safe_manual_pipeline_task"}}
        result = observation.evaluate(samples, manual)
        self.assertTrue(result["passed"])
        self.assertEqual(result["failure_reasons"], [])

    def test_memory_growth_redis_oom_lock_wait_and_difference_block_gate(self):
        samples = [sample(index, memory_growth=8 * 1024**2,
                          lag=1 if index >= 10 else 0,
                          differences=1 if index == 4 else 0)
                   for index in range(observation.SAMPLE_COUNT)]
        samples[-1]["redis"]["oom_error_count"] = 1
        samples[5]["mysql"]["Innodb_row_lock_current_waits"] = 1
        manual = {"tasks": {"metadata_reconciliation": "passed",
                            "schema_contract_reconciliation": "passed",
                            "status_summary": "passed"}}
        result = observation.evaluate(samples, manual)
        self.assertFalse(result["passed"])
        self.assertIn("unattributed_difference", result["failure_reasons"])
        self.assertIn("redis_capacity_or_growth", result["failure_reasons"])
        self.assertIn("mysql_connection_or_lock_trend", result["failure_reasons"])
        self.assertIn("kafka_lag_not_converged", result["failure_reasons"])

    def test_historical_restart_and_redis_counters_are_allowed_when_unchanged(self):
        samples = [sample(index) for index in range(observation.SAMPLE_COUNT)]
        for item in samples:
            for container in item["containers"].values():
                container["restart_count"] = 3
            item["redis"]["oom_error_count"] = 2
            item["redis"]["evicted_keys"] = 4
        manual = {"tasks": {"metadata_reconciliation": "passed",
                            "schema_contract_reconciliation": "passed",
                            "status_summary": "passed"}}
        result = observation.evaluate(samples, manual)
        self.assertTrue(result["passed"])
        monitor = result["trends"]["containers"][observation.PIPELINE_CONTAINERS["monitor"]]
        self.assertEqual(monitor["first_restart_count"], 3)
        self.assertEqual(monitor["last_restart_count"], 3)
        self.assertEqual(result["trends"]["redis"]["first_oom_error_count"], 2)

    def test_restart_or_counter_increase_during_observation_blocks_gate(self):
        samples = [sample(index) for index in range(observation.SAMPLE_COUNT)]
        samples[-1]["containers"][observation.PIPELINE_CONTAINERS["relay"]]["restart_count"] = 1
        samples[-1]["redis"]["oom_error_count"] = 1
        manual = {"tasks": {"metadata_reconciliation": "passed",
                            "schema_contract_reconciliation": "passed",
                            "status_summary": "passed"}}
        result = observation.evaluate(samples, manual)
        self.assertFalse(result["passed"])
        self.assertIn("container_restart_or_oom", result["failure_reasons"])
        self.assertIn("redis_capacity_or_growth", result["failure_reasons"])

    def test_incomplete_sample_set_is_rejected(self):
        with self.assertRaises(observation.ObservationError):
            observation.evaluate([sample(0)], {"tasks": {}})

    def test_start_is_bound_to_current_dev_run_and_systemd_unit(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            evidence = Path(root) / "evidence" / RUN_ID
            state.mkdir(); evidence.mkdir(parents=True)
            (state / "current.json").write_text(json.dumps({
                "environment": "development", "project": observation.PROJECT,
                "run_id": RUN_ID,
            }), encoding="utf-8")
            completed = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(observation, "STATE", state), \
                 patch.object(observation, "evidence_root", return_value=evidence), \
                 patch.object(observation.subprocess, "run", return_value=completed) as runner:
                result = observation.start(RUN_ID, OBSERVATION_ID)
            self.assertEqual(result["status"], "starting")
            self.assertEqual(result["samples_required"], 13)
            command = runner.call_args.args[0]
            self.assertEqual(command[0], "systemd-run")
            self.assertIn("--property=NoNewPrivileges=true", command)
            self.assertEqual(command[-3:], ["run", RUN_ID, OBSERVATION_ID])

    def test_gateway_installer_and_workflow_expose_only_fixed_observation_commands(self):
        repo = Path(__file__).parents[2]
        root = repo / "deploy/environments/event_pipeline"
        wrapper = (root / "binhu-dev-event-pipeline-gateway").read_text(encoding="utf-8")
        gateway = (root / "binhu-dev-event-pipeline-gateway.py").read_text(encoding="utf-8")
        installer = (root / "install-dev-gateway.sh").read_text(encoding="utf-8")
        workflow = (repo / ".github/workflows/observe-dev-event-pipeline.yml").read_text(encoding="utf-8")
        self.assertIn("observe-start|observe-status", wrapper)
        self.assertIn('action in {"observe-start", "observe-status"}', gateway)
        self.assertIn("binhu-dev-event-pipeline-observation.py", installer)
        self.assertIn('"observe-$ACTION $RUN_ID $OBSERVATION_ID"', workflow)
        self.assertNotIn("Production", workflow)
        self.assertNotIn("Staging", workflow)

    def test_manual_reconciliation_uses_fixed_container_root_for_private_evidence(self):
        with patch.object(observation, "checked", return_value="{}") as runner, \
             patch.object(observation, "_monitor_status", return_value={"age_seconds": 1}), \
             patch.object(observation, "_kafka_lag", return_value={"lag": 0}), \
             patch.object(observation, "_json_command", return_value={"running_job_count": 1}):
            result = observation.trigger_manual_tasks(RUN_ID, Path(OBSERVATION_ID))
        self.assertEqual(result["tasks"]["metadata_reconciliation"], "passed")
        first_command = runner.call_args_list[0].args[0]
        self.assertEqual(first_command[:5], ["docker", "exec", "--user", "0:0",
                                             observation.PIPELINE_CONTAINERS["monitor"]])


if __name__ == "__main__":
    unittest.main()
