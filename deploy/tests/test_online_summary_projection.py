import unittest
from deploy.environments.event_pipeline.services.online_summary_projection import project_events
from deploy.environments.event_pipeline.services.python_online_summary_projection import upsert_sql


def event(event_id, task_id="t_fullchain:42"):
    return {"schema_version": 1, "event_id": event_id, "event_type": "task.saved",
            "task_id": task_id, "source_id": 42, "revision": 1, "operation_id": event_id,
            "changed_fields": ["task_state", "daily_report"], "timestamp": "2026-10-26T00:00:00Z",
            "environment": "development", "run_id": "dev-online-summary-1"}


class OnlineSummaryProjectionTests(unittest.TestCase):
    def test_groups_events_by_date_and_type_and_deduplicates(self):
        rows = project_events([event("00000000-0000-0000-0000-000000000001"),
                               event("00000000-0000-0000-0000-000000000001"),
                               event("00000000-0000-0000-0000-000000000002", "t_fullchain:43")])
        row = next(iter(rows.values()))
        self.assertEqual(row["event_count"], 2)
        self.assertEqual(row["task_count"], 2)
        self.assertEqual(row["changed_field_count"], 4)

    def test_sink_is_run_scoped_and_parameterized(self):
        row = next(iter(project_events([event("00000000-0000-0000-0000-000000000001")]).values()))
        sql, params = upsert_sql(row)
        self.assertIn("dev_online_summary_projection_python", sql)
        self.assertEqual(params[0], "dev-online-summary-1")
        self.assertNotIn("DROP", sql.upper())


if __name__ == "__main__":
    unittest.main()
