from __future__ import annotations

import unittest
from unittest.mock import patch

from deploy.environments.event_pipeline import flink_submission


RUN_ID = "dev-20260915-dualtrack-monitor08"


def sql(run_id: str = RUN_ID, group_id: str | None = None) -> str:
    group_id = group_id or f"{run_id}-flink"
    return f"""
SET 'pipeline.name' = '{run_id}';
CREATE TABLE dev_events (event_id STRING, task_id STRING, source_id BIGINT,
  revision BIGINT, environment STRING, run_id STRING) WITH (
  'connector' = 'kafka', 'topic' = 'dev.task.events.v1',
  'properties.group.id' = '{group_id}');
INSERT INTO dev_revisions
SELECT run_id, task_id, source_id, MAX(revision)
FROM dev_events
WHERE environment = 'development' AND run_id = '{run_id}'
GROUP BY run_id, task_id, source_id;
INSERT INTO dev_task_metadata
SELECT run_id, task_id, source_id, MAX(revision)
FROM dev_events
WHERE environment = 'development' AND run_id = '{run_id}'
GROUP BY run_id, task_id, source_id;
"""


def job(name: str = RUN_ID, state: str = "RUNNING", sinks: tuple[str, ...] =
        ("dev_revisions", "dev_task_metadata")) -> dict:
    descriptions = [
        "Source: dev_events -> Calc where=[environment = 'development' and run_id = "
        f"'{name}'] topic=dev.task.events.v1",
    ]
    descriptions.extend(f"Sink: {sink}" for sink in sinks)
    return {
        "jid": f"jid-{name}",
        "name": name,
        "state": state,
        "plan": {"nodes": [{"description": description} for description in descriptions]},
    }


class FlinkSubmissionContractTests(unittest.TestCase):
    def test_rest_readiness_retries_only_the_fixed_startup_failure(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def overview(self):
                self.calls += 1
                if self.calls < 3:
                    raise ValueError("Flink REST request failed")
                return [job()]

        client = Client()
        with patch.object(flink_submission.time, "sleep") as sleep:
            self.assertEqual(
                flink_submission.wait_for_rest(client, timeout=5),
                [job()],
            )
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleep.call_count, 2)

        class InvalidClient:
            def overview(self):
                raise ValueError("invalid Flink REST path")

        with patch.object(flink_submission.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "invalid Flink REST path"):
                flink_submission.wait_for_rest(InvalidClient(), timeout=5)
        sleep.assert_not_called()

    def test_rest_readiness_has_a_fixed_timeout(self):
        class Client:
            def overview(self):
                raise ValueError("Flink REST request failed")

        with patch.object(flink_submission.time, "monotonic", side_effect=[10.0, 11.0]), \
                patch.object(flink_submission.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "Flink REST request failed"):
                flink_submission.wait_for_rest(Client(), timeout=0.5)
        sleep.assert_not_called()

    def test_runtime_wait_retries_a_transient_rest_transport_failure(self):
        class Client:
            def __init__(self):
                self.overview_calls = 0

            def overview(self):
                self.overview_calls += 1
                if self.overview_calls == 1:
                    raise ValueError("Flink REST request failed")
                return [job()]

            def details(self, _jid):
                return job()

        client = Client()
        with patch.object(flink_submission.time, "sleep") as sleep:
            report = flink_submission.wait_for_runtime(
                client,
                RUN_ID,
                lambda: [f"{RUN_ID}-flink"],
                timeout=5,
            )
        self.assertEqual(report["run_id"], RUN_ID)
        self.assertEqual(client.overview_calls, 2)
        sleep.assert_called_once_with(2)

    def test_upload_jar_accepts_only_checked_dev_paths(self):
        calls = []

        class Result:
            returncode = 0
            stdout = '{"status":"success","filename":"/tmp/dev-pipeline-job.jar"}'

        def runner(command, **kwargs):
            calls.append(command)
            return Result()

        client = flink_submission.FlinkRest("dev-jobmanager", runner=runner)
        self.assertEqual(client.upload_jar("/tmp/dev-pipeline-job.jar"), "dev-pipeline-job.jar")
        with self.assertRaisesRegex(ValueError, "unexpected Dev Flink JAR path"):
            client.upload_jar("/tmp/other.jar")
        self.assertIn("path=@/tmp/dev-pipeline-job.jar", calls[0])

    def test_runtime_identity_is_parsed_as_exact_key_values(self):
        self.assertEqual(
            flink_submission.parse_runtime_identity(
                "APP_ENVIRONMENT=development\nDEV_RUN_ID=" + RUN_ID + "\n"
            ),
            RUN_ID,
        )
        with self.assertRaisesRegex(ValueError, "run_id"):
            flink_submission.parse_runtime_identity(
                "APP_ENVIRONMENT=development\nOTHER=DEV_RUN_ID=" + RUN_ID + "\n"
            )
        with self.assertRaisesRegex(ValueError, "development"):
            flink_submission.parse_runtime_identity(
                "APP_ENVIRONMENT=production\nDEV_RUN_ID=" + RUN_ID + "\n"
            )

    def test_sql_identity_must_match_manifest_run_id_and_group(self):
        identity = flink_submission.parse_sql_identity(sql())
        self.assertEqual(identity["run_id"], RUN_ID)
        self.assertEqual(identity["consumer_group"], f"{RUN_ID}-flink")
        with self.assertRaisesRegex(ValueError, "run_id"):
            flink_submission.validate_sql_identity(sql("dev-stale"), RUN_ID)
        with self.assertRaisesRegex(ValueError, "consumer group"):
            flink_submission.validate_sql_identity(sql(group_id="dev-stale-flink"), RUN_ID)

    def test_job_graph_rejects_old_run_id_or_non_running_job(self):
        flink_submission.validate_job_graph(job(), RUN_ID)
        with self.assertRaisesRegex(ValueError, "run_id"):
            flink_submission.validate_job_graph(job("dev-stale"), RUN_ID)
        with self.assertRaisesRegex(ValueError, "RUNNING"):
            flink_submission.validate_job_graph(job(state="CANCELED"), RUN_ID)

    def test_job_graph_requires_an_explicit_development_filter(self):
        bad = job()
        bad["plan"]["nodes"][0]["description"] = (
            "Source: dev_events -> Calc where=[run_id = '" + RUN_ID + "'] "
            "topic=dev.task.events.v1 Sink: dev_revisions Sink: dev_task_metadata"
        )
        with self.assertRaisesRegex(ValueError, "environment"):
            flink_submission.validate_job_graph(bad, RUN_ID)

    def test_job_graph_requires_both_insert_sinks(self):
        with self.assertRaisesRegex(ValueError, "missing INSERT sink"):
            flink_submission.validate_job_graph(job(sinks=()), RUN_ID)

    def test_active_jobs_are_partitioned_without_touching_other_dev_jobs(self):
        current = job()
        stale_monitor = job("dev-20260915-dualtrack-monitor")
        other_dev_job = job("dev-20260915-metadata08")
        active, stale = flink_submission.partition_active_jobs(
            [current, stale_monitor, other_dev_job], RUN_ID
        )
        self.assertEqual([item["jid"] for item in active], [current["jid"]])
        self.assertEqual([item["jid"] for item in stale], [stale_monitor["jid"]])

        finished = job("dev-20260915-dualtrack-monitor")
        finished["state"] = "CANCELED"
        active, stale = flink_submission.partition_active_jobs([finished], RUN_ID)
        self.assertEqual(active, [])
        self.assertEqual(stale, [])

    def test_runtime_requires_one_matching_job_with_both_sinks_and_group(self):
        report = flink_submission.validate_runtime(
            [job(RUN_ID)],
            [f"{RUN_ID}-flink"], RUN_ID
        )
        self.assertEqual(report["job_count"], 1)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            flink_submission.validate_runtime([job(), job("second")], [f"{RUN_ID}-flink"], RUN_ID)
        with self.assertRaisesRegex(ValueError, "consumer group"):
            flink_submission.validate_runtime(
                [job(RUN_ID)], ["dev-stale-flink"], RUN_ID
            )
        with self.assertRaisesRegex(ValueError, "missing INSERT sink"):
            flink_submission.validate_runtime(
                [job(RUN_ID, sinks=("dev_revisions",))], [f"{RUN_ID}-flink"], RUN_ID
            )

    def test_stale_job_clear_waits_before_new_submission(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def overview(self):
                self.calls += 1
                if self.calls == 1:
                    return [job("dev-20260915-dualtrack-monitor")]
                return []

        with patch.object(flink_submission.time, "sleep"):
            flink_submission.wait_for_stale_clear(Client(), RUN_ID, timeout=2)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            duplicate = job()
            flink_submission.validate_runtime([duplicate, duplicate], [f"{RUN_ID}-flink"], RUN_ID)


if __name__ == "__main__":
    unittest.main()
