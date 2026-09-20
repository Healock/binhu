"""Render a bounded, isolated Dev or Staging metadata aggregation job."""
from .runtime import configuration


def render(environ):
    config = configuration(environ)
    run = config["RUN_ID"]
    environment = config["APP_ENVIRONMENT"]
    checkpoint_scope = "dev" if environment == "development" else "staging"
    password = config["MYSQL_PASSWORD"]  # Validated hex; saved only server-private.
    return f"""SET 'execution.checkpointing.interval' = '10 s';
SET 'execution.checkpointing.mode' = 'EXACTLY_ONCE';
SET 'execution.checkpointing.externalized-checkpoint-retention' = 'RETAIN_ON_CANCELLATION';
SET 'state.checkpoints.dir' = 'file:///opt/flink/checkpoints/{checkpoint_scope}-pipeline';
SET 'state.savepoints.dir' = 'file:///opt/flink/checkpoints/{checkpoint_scope}-savepoints';
SET 'parallelism.default' = '1';
SET 'pipeline.name' = '{run}';
CREATE TABLE dev_events (
 schema_version INT, event_id STRING, event_type STRING, task_id STRING,
 source_id BIGINT, revision BIGINT, operation_id STRING,
 changed_fields ARRAY<STRING>, `timestamp` STRING, environment STRING, run_id STRING
) WITH (
 'connector' = 'kafka', 'topic' = '{config["TOPIC"]}',
 'properties.bootstrap.servers' = '{config["KAFKA_BOOTSTRAP_SERVERS"]}',
 'properties.group.id' = '{run}-flink', 'scan.startup.mode' = 'earliest-offset',
 'format' = 'json', 'json.fail-on-missing-field' = 'true',
 'json.ignore-parse-errors' = 'false'
);
CREATE TABLE dev_revisions (
 run_id VARCHAR(80), task_id VARCHAR(96), source_id BIGINT, revision BIGINT,
 PRIMARY KEY (run_id, task_id, source_id) NOT ENFORCED
) WITH (
 'connector' = 'jdbc',
 'url' = 'jdbc:mysql://{config["MYSQL_HOST"]}:3306/{config["MYSQL_DATABASE"]}?autoReconnect=true&maxReconnects=3&initialTimeout=2&tcpKeepAlive=true&connectTimeout=5000&socketTimeout=15000',
 'table-name' = 'dev_task_revisions', 'username' = '{config["MYSQL_USER"]}',
 'password' = '{password}', 'sink.buffer-flush.interval' = '1 s',
 'sink.buffer-flush.max-rows' = '100', 'sink.max-retries' = '3'
);
INSERT INTO dev_revisions
SELECT run_id, task_id, source_id, MAX(revision)
FROM dev_events
WHERE environment = '{environment}' AND run_id = '{run}'
 AND schema_version = 1 AND source_id > 0 AND revision >= 0
GROUP BY run_id, task_id, source_id;

CREATE TABLE dev_task_metadata (
 run_id VARCHAR(80), task_id VARCHAR(96), source_id BIGINT, revision BIGINT,
 event_count BIGINT, changed_field_count BIGINT,
 created_count BIGINT, saved_count BIGINT, claimed_count BIGINT,
 assigned_count BIGINT, reviewed_count BIGINT, archived_count BIGINT,
 deleted_count BIGINT,
 PRIMARY KEY (run_id, task_id, source_id) NOT ENFORCED
) WITH (
 'connector' = 'jdbc',
 'url' = 'jdbc:mysql://{config["MYSQL_HOST"]}:3306/{config["MYSQL_DATABASE"]}?autoReconnect=true&maxReconnects=3&initialTimeout=2&tcpKeepAlive=true&connectTimeout=5000&socketTimeout=15000',
 'table-name' = 'dev_task_metadata', 'username' = '{config["MYSQL_USER"]}',
 'password' = '{password}', 'sink.buffer-flush.interval' = '1 s',
 'sink.buffer-flush.max-rows' = '100', 'sink.max-retries' = '3'
);
CREATE VIEW dev_unique_events AS
SELECT DISTINCT schema_version, event_id, event_type, task_id, source_id, revision,
       operation_id, changed_fields, `timestamp`, environment, run_id
FROM dev_events
WHERE environment = '{environment}' AND run_id = '{run}'
  AND schema_version = 1 AND source_id > 0 AND revision >= 0;
INSERT INTO dev_task_metadata
SELECT run_id, task_id, source_id, MAX(revision), COUNT(DISTINCT event_id),
 SUM(CARDINALITY(changed_fields)),
 COUNT(DISTINCT CASE WHEN event_type='task.created' THEN event_id END),
 COUNT(DISTINCT CASE WHEN event_type='task.saved' THEN event_id END),
 COUNT(DISTINCT CASE WHEN event_type='task.claimed' THEN event_id END),
 COUNT(DISTINCT CASE WHEN event_type='task.assigned' THEN event_id END),
 COUNT(DISTINCT CASE WHEN event_type='task.reviewed' THEN event_id END),
 COUNT(DISTINCT CASE WHEN event_type='task.archived' THEN event_id END),
 COUNT(DISTINCT CASE WHEN event_type='task.deleted' THEN event_id END)
FROM dev_unique_events
GROUP BY run_id, task_id, source_id;
"""
