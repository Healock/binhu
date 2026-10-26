import unittest

from deploy.environments.event_pipeline.services.online_source_projection import (
    OnlineSourceProjectionConflict,
    flatten_projection,
    project_events,
)
from deploy.environments.event_pipeline.services.python_online_source_projection import upsert_sql


def event(event_id, revision, changed_fields):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": "task.saved",
        "task_id": "t_fullchain:42",
        "source_id": 42,
        "revision": revision,
        "operation_id": event_id,
        "changed_fields": changed_fields,
        "timestamp": "2026-10-26T00:00:00Z",
        "environment": "development",
        "run_id": "dev-online-source-1",
    }


class OnlineSourceProjectionTests(unittest.TestCase):
    def test_projection_contains_only_revision_hints(self):
        rows = project_events([
            event("00000000-0000-0000-0000-000000000001", 4, ["address", "task_graph"]),
            event("00000000-0000-0000-0000-000000000002", 7, ["person_tags", "inspector"]),
        ])
        row = flatten_projection(next(iter(rows.values())), source="python")
        self.assertEqual(row["revision"], 7)
        self.assertEqual(row["address_match_revision"], 4)
        self.assertEqual(row["task_graph_revision"], 4)
        self.assertEqual(row["watch_snapshot_revision"], 7)
        self.assertEqual(row["assignment_revision"], 7)
        self.assertNotIn("address", row)
        self.assertNotIn("name", row)

    def test_duplicate_event_is_idempotent(self):
        item = event("00000000-0000-0000-0000-000000000001", 4, ["community"])
        row = project_events([item, item])
        self.assertEqual(next(iter(row.values()))["event_count"], 1)

    def test_reused_event_id_with_different_metadata_is_rejected(self):
        first = event("00000000-0000-0000-0000-000000000001", 4, ["community"])
        second = event("00000000-0000-0000-0000-000000000001", 4, ["address"])
        with self.assertRaises(OnlineSourceProjectionConflict):
            project_events([first, second])

    def test_python_sink_is_parameterized_and_run_scoped(self):
        row = next(iter(project_events([event(
            "00000000-0000-0000-0000-000000000001", 1, ["community"],
        )]).values()))
        sql, params = upsert_sql(row)
        self.assertIn("dev_online_source_projection_python", sql)
        self.assertNotIn("DROP", sql.upper())
        self.assertEqual(params[0], "dev-online-source-1")


if __name__ == "__main__":
    unittest.main()
