"""Metadata-only projection schemas shared by Flink and Staging bootstrap.

The definitions deliberately contain identifiers, revisions and counters only.
Business rows, person values, photos and external write-back payloads remain in
their authoritative services.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectionDefinition:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    select_sql: str


BASE_PROJECTION_NAMES = frozenset(("dev_revisions", "dev_task_metadata"))


PROJECTIONS: tuple[ProjectionDefinition, ...] = (
    ProjectionDefinition(
        "dev_online_source_projection",
        ("run_id VARCHAR(80)", "task_id VARCHAR(96)", "source_id BIGINT", "revision BIGINT",
         "event_count BIGINT", "address_match_revision BIGINT", "community_revision BIGINT",
         "assignment_revision BIGINT", "task_graph_revision BIGINT",
         "watch_snapshot_revision BIGINT", "last_event_type VARCHAR(40)"),
        ("run_id", "task_id", "source_id"),
        """SELECT run_id, task_id, source_id, MAX(revision), COUNT(DISTINCT event_id),
 MAX(CASE WHEN ARRAY_CONTAINS(changed_fields, 'address') THEN revision ELSE 0 END),
 MAX(CASE WHEN ARRAY_CONTAINS(changed_fields, 'community') THEN revision ELSE 0 END),
 MAX(CASE WHEN ARRAY_CONTAINS(changed_fields, 'inspector') THEN revision ELSE 0 END),
 MAX(CASE WHEN ARRAY_CONTAINS(changed_fields, 'task_graph') THEN revision ELSE 0 END),
 MAX(CASE WHEN ARRAY_CONTAINS(changed_fields, 'person_tags') THEN revision ELSE 0 END),
 MAX_BY(event_type, revision)
FROM dev_unique_events
GROUP BY run_id, task_id, source_id""",
    ),
    ProjectionDefinition(
        "dev_online_summary_projection",
        ("run_id VARCHAR(80)", "business_date DATE", "event_type VARCHAR(40)",
         "event_count BIGINT", "task_count BIGINT", "changed_field_count BIGINT"),
        ("run_id", "business_date", "event_type"),
        """SELECT run_id, CAST(SUBSTRING(`timestamp`, 1, 10) AS DATE), event_type,
 COUNT(DISTINCT event_id), COUNT(DISTINCT CONCAT(task_id, ':', CAST(source_id AS STRING))),
 SUM(changed_field_count)
FROM dev_unique_events
GROUP BY run_id, CAST(SUBSTRING(`timestamp`, 1, 10) AS DATE), event_type""",
    ),
    ProjectionDefinition(
        "dev_realtime_event_projection",
        ("run_id VARCHAR(80)", "event_type VARCHAR(40)", "event_count BIGINT",
         "latest_revision BIGINT", "changed_field_count BIGINT"),
        ("run_id", "event_type"),
        """SELECT run_id, event_type, COUNT(DISTINCT event_id), MAX(revision),
 SUM(changed_field_count)
FROM dev_unique_events
GROUP BY run_id, event_type""",
    ),
    ProjectionDefinition(
        "dev_daily_ledger_projection",
        ("run_id VARCHAR(80)", "business_date DATE", "event_count BIGINT",
         "created_count BIGINT", "saved_count BIGINT", "completed_count BIGINT",
         "deleted_count BIGINT", "latest_revision BIGINT"),
        ("run_id", "business_date"),
        """SELECT run_id, CAST(SUBSTRING(`timestamp`, 1, 10) AS DATE), COUNT(DISTINCT event_id),
 SUM(CASE WHEN event_type='task.created' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.saved' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.reviewed' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.deleted' THEN 1 ELSE 0 END), MAX(revision)
FROM dev_unique_events
GROUP BY run_id, CAST(SUBSTRING(`timestamp`, 1, 10) AS DATE)""",
    ),
    ProjectionDefinition(
        "dev_police_dispatch_projection",
        ("run_id VARCHAR(80)", "business_date DATE", "dispatch_events BIGINT",
         "assignment_events BIGINT", "review_events BIGINT", "latest_revision BIGINT"),
        ("run_id", "business_date"),
        """SELECT run_id, CAST(SUBSTRING(`timestamp`, 1, 10) AS DATE),
 SUM(CASE WHEN event_type IN ('task.created','task.saved') THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.assigned' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.reviewed' THEN 1 ELSE 0 END), MAX(revision)
FROM dev_unique_events
GROUP BY run_id, CAST(SUBSTRING(`timestamp`, 1, 10) AS DATE)""",
    ),
    ProjectionDefinition(
        "dev_registry_projection",
        ("run_id VARCHAR(80)", "task_id VARCHAR(96)", "source_id BIGINT", "revision BIGINT",
         "registry_revision BIGINT", "household_revision BIGINT", "tag_revision BIGINT"),
        ("run_id", "task_id", "source_id"),
        """SELECT run_id, task_id, source_id, MAX(revision),
 MAX(CASE WHEN ARRAY_CONTAINS(changed_fields, 'community') THEN revision ELSE 0 END),
 MAX(CASE WHEN event_type IN ('task.created','task.deleted') THEN revision ELSE 0 END),
 MAX(CASE WHEN ARRAY_CONTAINS(changed_fields, 'person_tags') THEN revision ELSE 0 END)
FROM dev_unique_events
GROUP BY run_id, task_id, source_id""",
    ),
    ProjectionDefinition(
        "dev_qmf_projection",
        ("run_id VARCHAR(80)", "acquisition_events BIGINT", "registration_events BIGINT",
         "latest_revision BIGINT"),
        ("run_id",),
        """SELECT run_id,
 SUM(CASE WHEN event_type IN ('task.created','task.saved') THEN 1 ELSE 0 END),
 SUM(CASE WHEN ARRAY_CONTAINS(changed_fields, 'community') OR event_type='task.assigned' THEN 1 ELSE 0 END),
 MAX(revision)
FROM dev_unique_events
GROUP BY run_id""",
    ),
    ProjectionDefinition(
        "dev_visit_attendance_projection",
        ("run_id VARCHAR(80)", "attendance_events BIGINT", "review_events BIGINT",
         "assignment_events BIGINT", "latest_revision BIGINT"),
        ("run_id",),
        """SELECT run_id,
 SUM(CASE WHEN ARRAY_CONTAINS(changed_fields, 'inspector') THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.reviewed' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.assigned' THEN 1 ELSE 0 END), MAX(revision)
FROM dev_unique_events
GROUP BY run_id""",
    ),
    ProjectionDefinition(
        "dev_process_workflow_projection",
        ("run_id VARCHAR(80)", "claimed BIGINT", "assigned BIGINT", "reviewed BIGINT",
         "saved BIGINT", "latest_revision BIGINT"),
        ("run_id",),
        """SELECT run_id,
 SUM(CASE WHEN event_type='task.claimed' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.assigned' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.reviewed' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='task.saved' THEN 1 ELSE 0 END), MAX(revision)
FROM dev_unique_events
GROUP BY run_id""",
    ),
    ProjectionDefinition(
        "dev_photo_projection",
        ("run_id VARCHAR(80)", "requested BIGINT", "attached BIGINT", "latest_revision BIGINT"),
        ("run_id",),
        """SELECT run_id,
 SUM(CASE WHEN event_type='photo.requested' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='photo.attached' THEN 1 ELSE 0 END), MAX(revision)
FROM dev_unique_events
GROUP BY run_id""",
    ),
    ProjectionDefinition(
        "dev_venue_external_projection",
        ("run_id VARCHAR(80)", "venue_visits BIGINT", "external_updates BIGINT", "latest_revision BIGINT"),
        ("run_id",),
        """SELECT run_id,
 SUM(CASE WHEN event_type='venue.visited' THEN 1 ELSE 0 END),
 SUM(CASE WHEN event_type='external.stats.updated' THEN 1 ELSE 0 END), MAX(revision)
FROM dev_unique_events
GROUP BY run_id""",
    ),
)


def projection_names() -> frozenset[str]:
    return frozenset(BASE_PROJECTION_NAMES | {item.name for item in PROJECTIONS})


def flink_sql(config: dict[str, str]) -> str:
    password = config["MYSQL_PASSWORD"]
    jdbc = (
        f"jdbc:mysql://{config['MYSQL_HOST']}:3306/{config['MYSQL_DATABASE']}"
        "?autoReconnect=true&maxReconnects=3&initialTimeout=2&tcpKeepAlive=true"
        "&connectTimeout=5000&socketTimeout=15000"
    )
    blocks: list[str] = []
    for item in PROJECTIONS:
        columns = ", ".join(item.columns)
        primary = ", ".join(item.primary_key)
        blocks.append(
            f"CREATE TABLE {item.name} (\n {columns},\n"
            f" PRIMARY KEY ({primary}) NOT ENFORCED\n) WITH (\n"
            f" 'connector' = 'jdbc', 'url' = '{jdbc}',\n"
            f" 'table-name' = '{item.name}', 'username' = '{config['MYSQL_USER']}',\n"
            f" 'password' = '{password}', 'sink.buffer-flush.interval' = '1 s',\n"
            " 'sink.buffer-flush.max-rows' = '100', 'sink.max-retries' = '3'\n);"
        )
        blocks.append(f"INSERT INTO {item.name}\n{item.select_sql};")
    return "\n".join(blocks)


def staging_schema_sql() -> str:
    blocks: list[str] = []
    for item in PROJECTIONS:
        columns = ", ".join(f"{column} NOT NULL" for column in item.columns)
        primary = ", ".join(item.primary_key)
        blocks.append(
            f"CREATE TABLE {item.name} (\n {columns},\n PRIMARY KEY ({primary})\n)"
            " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;\n"
            f"CREATE TABLE {item.name}_python LIKE {item.name};"
        )
    return "\n".join(blocks)


__all__ = ["BASE_PROJECTION_NAMES", "PROJECTIONS", "flink_sql", "projection_names", "staging_schema_sql"]
