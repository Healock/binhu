import unittest
from deploy.environments.event_pipeline.services.police_dispatch_projection import project_events


def event(event_id, kind):
    return {"schema_version": 1, "event_id": event_id, "event_type": kind, "task_id": "t_police_stats:8",
            "source_id": 8, "revision": 3, "operation_id": event_id, "changed_fields": ["task_state"],
            "timestamp": "2026-10-26T00:00:00Z", "environment": "development", "run_id": "dev-dispatch-1"}


class PoliceDispatchProjectionTests(unittest.TestCase):
    def test_routes_dispatch_categories_and_deduplicates(self):
        rows = project_events([event("00000000-0000-0000-0000-000000000001", "task.created"),
                               event("00000000-0000-0000-0000-000000000002", "task.assigned"),
                               event("00000000-0000-0000-0000-000000000003", "task.reviewed")])
        row = next(iter(rows.values()))
        self.assertEqual((row["dispatch_events"], row["assignment_events"], row["review_events"]), (1, 1, 1))


if __name__ == "__main__": unittest.main()
