"""Render a bounded Dev metadata aggregation job with fixed connector targets."""
from .runtime import configuration


def render(environ):
    config = configuration(environ)
    run = config["DEV_RUN_ID"]
    password = config["MYSQL_PASSWORD"]  # Validated hex; saved only server-private.
    return f"""SET 'execution.checkpointing.interval' = '10 s';
SET 'execution.checkpointing.mode' = 'EXACTLY_ONCE';
SET 'execution.checkpointing.externalized-checkpoint-retention' = 'RETAIN_ON_CANCELLATION';
SET 'state.checkpoints.dir' = 'file:///opt/flink/checkpoints/dev-pipeline';
SET 'state.savepoints.dir' = 'file:///opt/flink/checkpoints/dev-savepoints';
SET 'parallelism.default' = '1';
SET 'pipeline.name' = '{run}';
CREATE TABLE dev_events (
 schema_version INT, event_id STRING, event_type STRING, task_id STRING,
 source_id BIGINT, revision BIGINT, operation_id STRING,
 changed_fields ARRAY<STRING>, `timestamp` STRING, environment STRING, run_id STRING
) WITH (
 'connector' = 'kafka', 'topic' = 'dev.task.events.v1',
 'properties.bootstrap.servers' = 'kafka-1:9092,kafka-2:9092,kafka-3:9092',
 'properties.group.id' = '{run}-flink', 'scan.startup.mode' = 'earliest-offset',
 'format' = 'json', 'json.fail-on-missing-field' = 'true',
 'json.ignore-parse-errors' = 'false'
);
CREATE TABLE dev_revisions (
 run_id VARCHAR(80), task_id VARCHAR(96), source_id BIGINT, revision BIGINT,
 PRIMARY KEY (run_id, task_id, source_id) NOT ENFORCED
) WITH (
 'connector' = 'jdbc',
 'url' = 'jdbc:mysql://dev-derived-mysql:3306/Dev_EventPipeline',
 'table-name' = 'dev_task_revisions', 'username' = 'dev_pipeline',
 'password' = '{password}', 'sink.buffer-flush.interval' = '1 s',
 'sink.buffer-flush.max-rows' = '100', 'sink.max-retries' = '3'
);
INSERT INTO dev_revisions
SELECT run_id, task_id, source_id, MAX(revision)
FROM dev_events
WHERE environment = 'development' AND run_id = '{run}'
 AND schema_version = 1 AND source_id > 0 AND revision >= 0
GROUP BY run_id, task_id, source_id;
"""
