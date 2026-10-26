import unittest
from deploy.environments.event_pipeline.services.daily_ledger_projection import project_events
from deploy.environments.event_pipeline.services.python_daily_ledger_projection import upsert_sql


def event(event_id, event_type):
    return {"schema_version": 1, "event_id": event_id, "event_type": event_type,
            "task_id": "t_fullchain:42", "source_id": 42, "revision": 2,
            "operation_id": event_id, "changed_fields": ["daily_report"],
            "timestamp": "2026-10-26T00:00:00Z", "environment": "development",
            "run_id": "dev-daily-1"}


class DailyLedgerTests(unittest.TestCase):
    def test_counts_are_idempotent_and_revision_bounded(self):
        rows = project_events([event("00000000-0000-0000-0000-000000000001", "task.created"),
                               event("00000000-0000-0000-0000-000000000002", "task.reviewed"),
                               event("00000000-0000-0000-0000-000000000002", "task.reviewed")])
        row = next(iter(rows.values()))
        self.assertEqual(row["event_count"], 2)
        self.assertEqual(row["created_count"], 1)
        self.assertEqual(row["completed_count"], 1)
        self.assertEqual(row["latest_revision"], 2)

    def test_upsert_is_parameterized(self):
        row = next(iter(project_events([event("00000000-0000-0000-0000-000000000001", "task.saved")]).values()))
        sql, params = upsert_sql(row)
        self.assertIn("dev_daily_ledger_projection_python", sql)
        self.assertEqual(params[0], "dev-daily-1")


if __name__ == "__main__": unittest.main()
