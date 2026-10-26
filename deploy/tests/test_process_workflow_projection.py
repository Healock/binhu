import unittest
from deploy.environments.event_pipeline.services.process_workflow_projection import project_events


def event(event_id, kind):
    return {"schema_version": 1, "event_id": event_id, "event_type": kind, "task_id": "t_fullchain:9", "source_id": 9,
            "revision": 1, "operation_id": event_id, "changed_fields": ["task_state"], "timestamp": "2026-10-26T00:00:00Z",
            "environment": "development", "run_id": "dev-process-1"}


class ProcessWorkflowTests(unittest.TestCase):
    def test_workflow_state_event_counters(self):
        row = project_events([event("00000000-0000-0000-0000-000000000001", "task.claimed"),
                              event("00000000-0000-0000-0000-000000000002", "task.assigned")])["dev-process-1"]
        self.assertEqual((row["claimed"], row["assigned"]), (1, 1))


if __name__ == "__main__": unittest.main()
