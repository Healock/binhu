import unittest
from deploy.environments.event_pipeline.services.realtime_event_projection import project_events
from deploy.environments.event_pipeline.services.python_realtime_event_projection import upsert_sql


def event(event_id, revision=1):
    return {"schema_version": 1, "event_id": event_id, "event_type": "task.saved",
            "task_id": "t_fullchain:42", "source_id": 42, "revision": revision,
            "operation_id": event_id, "changed_fields": ["task_state"],
            "timestamp": "2026-10-26T00:00:00Z", "environment": "development",
            "run_id": "dev-realtime-1"}


class RealtimeProjectionTests(unittest.TestCase):
    def test_duplicate_notification_event_is_idempotent(self):
        rows = project_events([event("00000000-0000-0000-0000-000000000001"),
                               event("00000000-0000-0000-0000-000000000001")])
        self.assertEqual(next(iter(rows.values()))["event_count"], 1)

    def test_revision_watermark_and_parameterized_sink(self):
        row = next(iter(project_events([event("00000000-0000-0000-0000-000000000001", 8)]).values()))
        self.assertEqual(row["latest_revision"], 8)
        sql, params = upsert_sql(row)
        self.assertIn("dev_realtime_event_projection_python", sql)
        self.assertEqual(params[0], "dev-realtime-1")


if __name__ == "__main__":
    unittest.main()
