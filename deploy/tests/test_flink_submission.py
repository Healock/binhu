from __future__ import annotations

import unittest
from unittest.mock import patch

from deploy.environments.event_pipeline import flink_sql, flink_submission


RUN_ID = "dev-20260915-dualtrack-monitor08"


def sql(run_id: str = RUN_ID, group_id: str | None = None) -> str:
    group_id = group_id or f"{run_id}-flink"
    return f"""
SET 'pipeline.name' = '{run_id}';
CREATE TABLE dev_events (event_id STRING, task_id STRING, source_id BIGINT,
  revision BIGINT, environment STRING, run_id STRING) WITH (
  'connector' = 'kafka', 'topic' = 'dev.task.read-model.v4',
  'properties.group.id' = '{group_id}');
CREATE TABLE dev_task_events_redis (event_id STRING, task_id STRING, source_id BIGINT,
  revision BIGINT, environment STRING, run_id STRING) WITH (
  'connector' = 'kafka', 'topic' = 'dev.task.read-model.v4',
  'properties.group.id' = '{run_id}-flink-redis');
INSERT INTO dev_revisions
SELECT run_id, task_id, source_id, MAX(revision)
FROM dev_events
WHERE environment = 'development' AND run_id = '{run_id}'
 AND (schema_version = 4 OR schema_version = 6)
GROUP BY run_id, task_id, source_id;
INSERT INTO dev_task_metadata
SELECT run_id, task_id, source_id, MAX(revision)
FROM dev_events
WHERE environment = 'development' AND run_id = '{run_id}'
 AND (schema_version = 4 OR schema_version = 6)
GROUP BY run_id, task_id, source_id;
"""


def job(name: str = RUN_ID, state: str = "RUNNING", sinks: tuple[str, ...] =
        ("dev_revisions", "dev_task_metadata", "task-read-model-redis")) -> dict:
    descriptions = [
        "Source: dev_events[1] -> (Calc[2], Calc[7])",
        "Source: dev_task_events_redis[1] -> Calc[11]",
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
                    raise flink_submission.FlinkRestError("flink_rest_transport_failed", "/jobs/overview")
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

    def test_job_attaches_redis_sink_to_the_submitted_stream_graph(self):
        source = open("deploy/environments/event_pipeline/PipelineJob.java", encoding="utf-8").read()
        self.assertIn("StreamTableEnvironment.create", source)
        self.assertIn("statementSet.attachAsDataStream()", source)
        self.assertIn("stream.execute(runId)", source)
        self.assertIn("FROM dev_task_events_redis", source)
        self.assertIn("(schema_version=4 OR schema_version=5 OR schema_version=6)", source)
        self.assertIn('runId.matches("dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}")', source)

    def test_rest_readiness_has_a_fixed_timeout(self):
        class Client:
            def overview(self):
                raise flink_submission.FlinkRestError("flink_rest_timeout", "/jobs/overview")

        with patch.object(flink_submission.time, "monotonic", side_effect=[10.0, 11.0]), \
                patch.object(flink_submission.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "flink_rest_timeout"):
                flink_submission.wait_for_rest(Client(), timeout=0.5)
        sleep.assert_not_called()

    def test_runtime_wait_retries_a_transient_rest_transport_failure(self):
        class Client:
            def __init__(self):
                self.overview_calls = 0

            def overview(self):
                self.overview_calls += 1
                if self.overview_calls == 1:
                    raise flink_submission.FlinkRestError("flink_rest_transport_failed", "/jobs/overview")
                return [job()]

            def details(self, _jid):
                return job()

        client = Client()
        with patch.object(flink_submission.time, "sleep") as sleep:
            report = flink_submission.wait_for_runtime(
                client,
                RUN_ID,
                lambda: [f"{RUN_ID}-flink", f"{RUN_ID}-flink-redis"],
                timeout=5,
            )
        self.assertEqual(report["run_id"], RUN_ID)
        self.assertEqual(client.overview_calls, 2)
        sleep.assert_called_once_with(2)

    def test_upload_jar_accepts_only_checked_dev_paths(self):
        calls = []

        class Result:
            returncode = 0
            stdout = '{"status":"success","filename":"/tmp/dev-pipeline-job.jar"}\n__BINHU_HTTP_STATUS__:200'

        def runner(command, **kwargs):
            calls.append(command)
            return Result()

        client = flink_submission.FlinkRest("dev-jobmanager", runner=runner)
        self.assertEqual(client.upload_jar("/tmp/dev-pipeline-job.jar"), "dev-pipeline-job.jar")
        with self.assertRaisesRegex(ValueError, "unexpected Dev Flink JAR path"):
            client.upload_jar("/tmp/other.jar")
        self.assertIn("path=@/tmp/dev-pipeline-job.jar", calls[0])

    def test_uncertain_submission_reconciles_running_job_without_resubmit(self):
        class Client:
            def __init__(self):
                self.run_calls = 0
                self.overview_calls = 0

            def run_jar(self, _jar_id):
                self.run_calls += 1
                raise flink_submission.FlinkRestError(
                    "flink_rest_transport_failed", "/jars/job.jar/run",
                )

            def overview(self):
                self.overview_calls += 1
                return [] if self.overview_calls == 1 else [job()]

        client = Client()
        with patch.object(flink_submission.time, "sleep") as sleep:
            result = flink_submission.submit_jar_or_reconcile(client, "job.jar", RUN_ID, timeout=5)
        self.assertEqual(result, {"mode": "reconciled", "job_id": job()["jid"]})
        self.assertEqual(client.run_calls, 1)
        self.assertEqual(client.overview_calls, 2)
        sleep.assert_called_once_with(1)

    def test_uncertain_submission_preserves_transport_error_when_no_job_appears(self):
        class Client:
            def run_jar(self, _jar_id):
                raise flink_submission.FlinkRestError(
                    "flink_rest_timeout", "/jars/job.jar/run",
                )

            def overview(self):
                return []

        with patch.object(flink_submission.time, "monotonic", side_effect=[0, 6]), \
                patch.object(flink_submission.time, "sleep"):
            with self.assertRaisesRegex(flink_submission.FlinkRestError, "flink_rest_timeout"):
                flink_submission.submit_jar_or_reconcile(Client(), "job.jar", RUN_ID, timeout=5)

    def test_uncertain_submission_allows_slow_jobgraph_startup(self):
        class Client:
            def __init__(self):
                self.run_calls = 0
                self.overview_calls = 0

            def run_jar(self, _jar_id):
                self.run_calls += 1
                raise flink_submission.FlinkRestError(
                    "flink_rest_transport_failed", "/jars/job.jar/run",
                )

            def overview(self):
                self.overview_calls += 1
                return [job()] if self.overview_calls >= 40 else []

        client = Client()
        with patch.object(flink_submission.time, "monotonic", side_effect=iter(range(200))), \
                patch.object(flink_submission.time, "sleep"):
            result = flink_submission.submit_jar_or_reconcile(client, "job.jar", RUN_ID)
        self.assertEqual(result, {"mode": "reconciled", "job_id": job()["jid"]})
        self.assertEqual(client.run_calls, 1)
        self.assertEqual(client.overview_calls, 40)

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
        self.assertEqual(identity["consumer_groups"], [f"{RUN_ID}-flink", f"{RUN_ID}-flink-redis"])
        with self.assertRaisesRegex(ValueError, "run_id"):
            flink_submission.validate_sql_identity(sql("dev-stale"), RUN_ID)
        with self.assertRaisesRegex(ValueError, "consumer group"):
            flink_submission.validate_sql_identity(sql(group_id="dev-stale-flink"), RUN_ID)

    def test_rendered_sql_matches_projection_schema_contract(self):
        rendered = flink_sql.render({
            "APP_ENVIRONMENT": "development",
            "DEV_RUN_ID": RUN_ID,
            "MYSQL_PASSWORD": "a" * 48,
            "REDIS_PASSWORD": "b" * 48,
            "MYSQL_HOST": "dev-derived-mysql",
            "MYSQL_DATABASE": "Dev_EventPipeline",
            "MYSQL_USER": "dev_pipeline",
            "REDIS_HOST": "dev-derived-redis",
            "KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9092,kafka-2:9092,kafka-3:9092",
            "BACKEND_REDIS_URL": "redis://:test-password@redis:6379/0",
        })
        identity = flink_submission.validate_sql_identity(rendered, RUN_ID)
        self.assertEqual(identity["run_id"], RUN_ID)
        self.assertEqual(identity["consumer_groups"], [
            f"{RUN_ID}-flink", f"{RUN_ID}-flink-redis",
        ])

    def test_sql_identity_ignores_non_kafka_tables_when_collecting_source_groups(self):
        with_jdbc_sink = sql() + """
CREATE TABLE dev_online_source_projection (
  run_id STRING, task_id STRING, source_id BIGINT
) WITH (
  'connector' = 'jdbc',
  'url' = 'jdbc:mysql://dev-derived-mysql:3306/Dev_EventPipeline'
);
"""
        identity = flink_submission.validate_sql_identity(with_jdbc_sink, RUN_ID)
        self.assertEqual(identity["consumer_groups"], [f"{RUN_ID}-flink", f"{RUN_ID}-flink-redis"])

    def test_sql_identity_exposes_all_projection_sinks(self):
        extra = sql() + "INSERT INTO dev_online_source_projection SELECT * FROM dev_events;"
        identity = flink_submission.parse_sql_identity(extra)
        self.assertIn("dev_online_source_projection", identity["sinks"])
        self.assertIn("dev_revisions", identity["sinks"])

    def test_runtime_can_require_an_additional_projection_sink(self):
        extra = job(sinks=("dev_revisions", "dev_task_metadata", "dev_online_source_projection",
                           "task-read-model-redis"))
        report = flink_submission.validate_runtime(
            [extra], [f"{RUN_ID}-flink", f"{RUN_ID}-flink-redis"], RUN_ID,
            expected_sinks=("dev_revisions", "dev_task_metadata", "dev_online_source_projection",
                            "task-read-model-redis"),
        )
        self.assertIn("dev_online_source_projection", report["sinks"])

    def test_sql_identity_rejects_missing_or_duplicate_redis_group(self):
        missing = sql().replace(f"'properties.group.id' = '{RUN_ID}-flink-redis');", "'properties.group.id' = '{RUN_ID}-flink');")
        with self.assertRaisesRegex(ValueError, "consumer groups"):
            flink_submission.validate_sql_identity(missing, RUN_ID)

    def test_sql_identity_requires_v4_filter(self):
        with self.assertRaisesRegex(ValueError, "schema version"):
            flink_submission.validate_sql_identity(sql().replace("schema_version = 4", "schema_version = 3"), RUN_ID)

    def test_job_graph_rejects_old_run_id_or_non_running_job(self):
        flink_submission.validate_job_graph(job(), RUN_ID)
        with self.assertRaisesRegex(ValueError, "run_id"):
            flink_submission.validate_job_graph(job("dev-stale"), RUN_ID)
        with self.assertRaisesRegex(ValueError, "RUNNING"):
            flink_submission.validate_job_graph(job(state="CANCELED"), RUN_ID)

    def test_job_graph_accepts_real_flink_1_20_operator_descriptions(self):
        self.assertEqual(flink_submission.validate_job_graph(job(), RUN_ID)["sinks"], [
            "dev_revisions", "dev_task_metadata", "task-read-model-redis",
        ])

    def test_job_graph_requires_exact_named_source_and_sinks(self):
        with self.assertRaisesRegex(ValueError, "sources do not match"):
            flink_submission.validate_job_graph(
                {**job(), "plan": {"nodes": [
                    {"description": "Sink: dev_revisions"},
                    {"description": "Sink: dev_task_metadata"},
                    {"description": "Sink: task-read-model-redis"},
                ]}}, RUN_ID,
            )
        with self.assertRaisesRegex(ValueError, "unexpected sink"):
            flink_submission.validate_job_graph(
                {**job(), "plan": {"nodes": [
                    {"description": "Source: dev_events[1] -> Calc"},
                    {"description": "Source: dev_task_events_redis[1] -> Calc"},
                    {"description": "Sink: dev_revisions_extra"},
                    {"description": "Sink: dev_task_metadata"},
                    {"description": "Sink: task-read-model-redis"},
                ]}}, RUN_ID,
            )

    def test_job_graph_uses_exact_job_name_for_run_identity(self):
        with self.assertRaisesRegex(ValueError, "run_id"):
            flink_submission.validate_job_graph(job("dev-stale"), RUN_ID)

    def test_rest_request_reports_status_without_response_body(self):
        class Result:
            returncode = 0
            stdout = '{"error":"private body"}\n__BINHU_HTTP_STATUS__:503'

        client = flink_submission.FlinkRest("jobmanager", runner=lambda *_args, **_kwargs: Result())
        with self.assertRaisesRegex(flink_submission.FlinkRestError, "flink_rest_http_503") as raised:
            client.request("/jobs/overview")
        self.assertEqual(raised.exception.diagnostic(), {
            "endpoint": "/jobs/overview", "error_code": "flink_rest_http_503", "http_status": 503,
        })
        self.assertNotIn("private body", str(raised.exception))

    def test_runtime_wait_preserves_validation_error_over_later_transport_error(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def overview(self):
                self.calls += 1
                if self.calls == 1:
                    return [job(sinks=("dev_revisions",))]
                raise flink_submission.FlinkRestError("flink_rest_transport_failed", "/jobs/overview")

            def details(self, _jid):
                return job(sinks=("dev_revisions",))

        with patch.object(flink_submission.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "missing INSERT sink"):
                flink_submission.wait_for_runtime(
                    Client(), RUN_ID, lambda: [f"{RUN_ID}-flink"], timeout=1,
                )

    def test_job_graph_requires_both_insert_sinks_and_redis_task_sink(self):
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
            [f"{RUN_ID}-flink", f"{RUN_ID}-flink-redis"], RUN_ID
        )
        self.assertEqual(report["job_count"], 1)
        self.assertEqual(report["consumer_groups"], [f"{RUN_ID}-flink", f"{RUN_ID}-flink-redis"])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            flink_submission.validate_runtime([job(), job("second")], [f"{RUN_ID}-flink", f"{RUN_ID}-flink-redis"], RUN_ID)
        with self.assertRaisesRegex(ValueError, "consumer group"):
            flink_submission.validate_runtime(
                [job(RUN_ID)], ["dev-stale-flink", "dev-stale-flink-redis"], RUN_ID
            )
        with self.assertRaisesRegex(ValueError, "missing INSERT sink"):
            flink_submission.validate_runtime(
                [job(RUN_ID, sinks=("dev_revisions",))], [f"{RUN_ID}-flink", f"{RUN_ID}-flink-redis"], RUN_ID
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
